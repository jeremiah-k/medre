"""CLI entry point: argument parser and command dispatch."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import platform
import sys

# Lightweight imports only — command modules are imported lazily inside
# dispatch branches so that ``--help``, ``version``, and ``config sample``
# never touch optional SDK packages (nio, meshtastic, RNS, LXMF).
from .exit_codes import EXIT_CONFIG


def _get_version() -> str:
    """Return the MEDRE version string."""
    try:
        return importlib.metadata.version("medre")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _version() -> None:
    """Print version, Python, and platform information."""
    version = _get_version()
    print(f"medre {version}")
    print(f"Python  {platform.python_version()}")
    print(f"Platform {platform.system()} {platform.release()} ({platform.machine()})")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="medre",
        description="Modular Event-driven Routing Engine",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Start the runtime (may send messages)")
    run_p.add_argument(
        "config_path",
        nargs="?",
        help="Path to config file (shorthand for --config)",
    )
    run_p.add_argument("--config", default=None, help="Path to config file")
    run_p.add_argument(
        "--snapshot-on-shutdown",
        default=None,
        metavar="PATH",
        help="Write final runtime snapshot JSON to PATH on graceful shutdown",
    )

    # config (with sub-subcommands)
    config_p = sub.add_parser("config", help="Config management commands")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    check_p = config_sub.add_parser("check", help="Validate config file")
    check_p.add_argument("--config", default=None, help="Path to config file")
    config_sub.add_parser("sample", help="Print sample config")

    # paths
    sub.add_parser("paths", help="Print resolved MEDRE paths")

    # version
    sub.add_parser("version", help="Print MEDRE version")

    # adapters
    sub.add_parser("adapters", help="List available and configured adapters")

    # diagnostics
    diag_p = sub.add_parser(
        "diagnostics",
        help="Print pre-flight runtime snapshot (read-only; requires --config)",
    )
    diag_p.add_argument("--config", default=None, help="Path to config file")
    diag_p.add_argument(
        "--refresh-health",
        action="store_true",
        default=False,
        help="Start runtime, refresh adapter health once, print live snapshot",
    )

    # routes (with sub-subcommands)
    routes_p = sub.add_parser("routes", help="Route management commands")
    routes_sub = routes_p.add_subparsers(dest="routes_command", required=True)
    routes_validate_p = routes_sub.add_parser(
        "validate", help="Validate route configuration"
    )
    routes_validate_p.add_argument("--config", default=None, help="Path to config file")
    routes_topology_p = routes_sub.add_parser(
        "topology", help="Print route topology preview"
    )
    routes_topology_p.add_argument("--config", default=None, help="Path to config file")
    routes_list_p = routes_sub.add_parser("list", help="List configured routes")
    routes_list_p.add_argument("--config", default=None, help="Path to config file")

    # smoke
    smoke_p = sub.add_parser(
        "smoke",
        help="Local validation tooling, not daily operation (developers/CI; accepts --storage-path)",
    )
    smoke_p.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: examples/configs/fake-bridge-smoke.toml)",
    )
    smoke_p.add_argument(
        "--message", default="medre smoke test", help="Text for test message"
    )
    smoke_p.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Persist smoke evidence to SQLite at this path (default: in-memory)",
    )
    smoke_p.add_argument(
        "--drill",
        default=None,
        metavar="NAME",
        help="Run named failure drill instead of normal smoke",
    )
    smoke_p.add_argument(
        "--run-session",
        action="store_true",
        default=False,
        help="Run a complete bridge session: start→inject→poll→stop→snapshot→report",
    )
    smoke_p.add_argument(
        "--snapshot-dir",
        default=None,
        metavar="DIR",
        help="Directory for final snapshot JSON (run-session only; default: storage directory)",
    )
    smoke_p.add_argument(
        "--scenario",
        default="happy_path",
        choices=[
            "happy_path",
            "renderer_failure",
            "adapter_permanent_failure",
            "adapter_transient_failure",
            "capacity_rejection",
            "degraded_live_health",
        ],
        help="Failure scenario for run-session (default: happy_path)",
    )
    smoke_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON report"
    )

    # evidence
    evidence_p = sub.add_parser(
        "evidence",
        help="Specialized support bundle, usually inspect event --evidence (read-only; accepts --storage-path)",
    )
    evidence_mx = evidence_p.add_mutually_exclusive_group()
    evidence_mx.add_argument("--config", default=None, help="Path to config file")
    evidence_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; collects storage/trace evidence only)",
    )
    evidence_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON report"
    )
    evidence_p.add_argument(
        "--event",
        default=None,
        metavar="EVENT_ID",
        help="Include event and delivery receipts from storage",
    )
    evidence_p.add_argument(
        "--replay-run",
        default=None,
        metavar="RUN_ID",
        help="Include delivery receipts for a replay run from storage",
    )
    evidence_p.add_argument(
        "--include-refresh-health",
        action="store_true",
        default=False,
        help="Start runtime once to refresh live adapter health",
    )

    # inspect (with sub-subcommands)
    inspect_p = sub.add_parser(
        "inspect",
        help="Primary read-only investigation command (accepts --storage-path)",
    )
    inspect_sub = inspect_p.add_subparsers(dest="inspect_command", required=True)

    # inspect event <event_id>
    inspect_evt = inspect_sub.add_parser("event", help="Inspect a canonical event")
    inspect_evt_mx = inspect_evt.add_mutually_exclusive_group()
    inspect_evt_mx.add_argument("--config", default=None, help="Path to config file")
    inspect_evt_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    inspect_evt.add_argument("event_id", help="Canonical event ID to look up")
    inspect_evt.add_argument(
        "--timeline",
        action="store_true",
        default=False,
        help="Include chronological timeline (covers trace event output)",
    )
    inspect_evt.add_argument(
        "--evidence",
        action="store_true",
        default=False,
        help="Include evidence bundle, covers evidence command output (read-only, no runtime start)",
    )
    inspect_evt.add_argument(
        "--recovery",
        action="store_true",
        default=False,
        help="Include recovery runbook with failure classification (covers recover command output)",
    )

    # inspect receipts (--event <id> | --replay-run <run_id>)
    inspect_rcpt = inspect_sub.add_parser("receipts", help="List delivery receipts")
    inspect_rcpt_mx = inspect_rcpt.add_mutually_exclusive_group()
    inspect_rcpt_mx.add_argument("--config", default=None, help="Path to config file")
    inspect_rcpt_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    inspect_rcpt_group = inspect_rcpt.add_mutually_exclusive_group(required=True)
    inspect_rcpt_group.add_argument(
        "--event",
        default=None,
        help="Event ID to query receipts for",
    )
    inspect_rcpt_group.add_argument(
        "--replay-run",
        default=None,
        help="Replay run ID to query receipts for",
    )

    # inspect native-ref --adapter A --message M [--channel C]
    inspect_nref = inspect_sub.add_parser(
        "native-ref", help="Resolve native ref to canonical event"
    )
    inspect_nref_mx = inspect_nref.add_mutually_exclusive_group()
    inspect_nref_mx.add_argument("--config", default=None, help="Path to config file")
    inspect_nref_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    inspect_nref.add_argument("--adapter", required=True, help="Adapter name")
    inspect_nref.add_argument(
        "--channel",
        default=None,
        help="Native channel ID (omit for channelless protocols)",
    )
    inspect_nref.add_argument("--message", required=True, help="Native message ID")

    # inspect replay <run_id>
    inspect_rpl = inspect_sub.add_parser(
        "replay", help="Inspect a replay run timeline (read-only)"
    )
    inspect_rpl_mx = inspect_rpl.add_mutually_exclusive_group()
    inspect_rpl_mx.add_argument("--config", default=None, help="Path to config file")
    inspect_rpl_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    inspect_rpl.add_argument("run_id", help="Replay run ID to inspect")

    # trace (with sub-subcommands)
    trace_p = sub.add_parser(
        "trace",
        help="Specialized timeline, usually inspect event --timeline (read-only; accepts --storage-path)",
    )
    trace_sub = trace_p.add_subparsers(dest="trace_command", required=True)

    # trace event <event_id>
    trace_evt = trace_sub.add_parser(
        "event", help="Assemble timeline for a canonical event"
    )
    trace_evt_mx = trace_evt.add_mutually_exclusive_group()
    trace_evt_mx.add_argument("--config", default=None, help="Path to config file")
    trace_evt_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    trace_evt.add_argument("event_id", help="Canonical event ID to trace")
    trace_evt.add_argument(
        "--json", action="store_true", default=False, help="Output JSON timeline"
    )

    # trace replay <run_id>
    trace_rpl = trace_sub.add_parser(
        "replay", help="Assemble timeline for a replay run"
    )
    trace_rpl_mx = trace_rpl.add_mutually_exclusive_group()
    trace_rpl_mx.add_argument("--config", default=None, help="Path to config file")
    trace_rpl_mx.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Path to SQLite database (read-only; no config required)",
    )
    trace_rpl.add_argument("run_id", help="Replay run ID to trace")
    trace_rpl.add_argument(
        "--json", action="store_true", default=False, help="Output JSON timeline"
    )

    # replay
    replay_p = sub.add_parser(
        "replay",
        help="Re-execute stored events; best_effort mode may send messages (requires --config)",
    )
    replay_p.add_argument("--config", default=None, help="Path to config file")
    replay_p.add_argument(
        "--mode",
        required=True,
        choices=["strict", "re_render", "re_route", "best_effort", "dry_run"],
        help="Replay mode",
    )
    replay_p.add_argument(
        "--event", default=None, metavar="EVENT_ID", help="Event ID to replay"
    )
    replay_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON report"
    )
    replay_p.add_argument(
        "--target-adapters",
        default=None,
        nargs="+",
        metavar="ADAPTER",
        help="Restrict replay to these adapter IDs",
    )
    replay_p.add_argument(
        "--route-ids",
        default=None,
        nargs="+",
        metavar="ROUTE",
        help="Restrict replay to these route IDs",
    )
    replay_p.add_argument(
        "--limit", type=int, default=100, help="Max events to replay (default: 100)"
    )
    replay_p.add_argument(
        "--storage-path",
        default=None,
        metavar="PATH",
        help=argparse.SUPPRESS,
    )

    # recover
    recover_p = sub.add_parser(
        "recover",
        help="Specialized recovery classification, usually inspect event --recovery (read-only; requires --config)",
    )
    recover_p.add_argument("--config", default=None, help="Path to config file")
    recover_p.add_argument(
        "--event", default=None, metavar="EVENT_ID", help="Event ID to analyze"
    )
    recover_p.add_argument(
        "--failed-only",
        action="store_true",
        default=False,
        help="Only include events with failed deliveries",
    )
    recover_p.add_argument(
        "--since",
        default=None,
        metavar="TIMESTAMP",
        help="Only consider events after this timestamp",
    )
    recover_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview recovery without side effects",
    )
    recover_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON runbook"
    )

    # Adapter/plugin contributed commands (adapter, plugin namespaces)
    from .contrib import register_builtin_contributors

    register_builtin_contributors(sub)

    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        from .run_commands import _run

        try:
            asyncio.run(
                _run(
                    args.config or getattr(args, "config_path", None),
                    snapshot_path=getattr(args, "snapshot_on_shutdown", None),
                )
            )
        except KeyboardInterrupt:
            pass
    elif args.command == "config":
        if args.config_command == "check":
            from .config_commands import _config_check

            _config_check(args.config)
        elif args.config_command == "sample":
            from medre.config.sample import generate_sample_config

            print(generate_sample_config())
    elif args.command == "paths":
        from .config_commands import _paths

        _paths()
    elif args.command == "version":
        _version()
    elif args.command == "adapters":
        from .config_commands import _adapters

        _adapters()
    elif args.command == "diagnostics":
        from .diagnostics_commands import _diagnostics, _diagnostics_refresh

        if getattr(args, "refresh_health", False):
            asyncio.run(_diagnostics_refresh(args.config))
        else:
            _diagnostics(args.config)
    elif args.command == "routes":
        from .route_commands import _routes_list, _routes_topology, _routes_validate

        if args.routes_command == "validate":
            _routes_validate(args.config)
        elif args.routes_command == "topology":
            _routes_topology(args.config)
        elif args.routes_command == "list":
            _routes_list(args.config)
    elif args.command == "smoke":
        from .smoke_commands import _run_session, _smoke

        if getattr(args, "run_session", False):
            asyncio.run(
                _run_session(
                    args.config,
                    storage_path=args.storage_path,
                    snapshot_dir=getattr(args, "snapshot_dir", None),
                    json_output=args.json,
                    scenario=getattr(args, "scenario", "happy_path"),
                )
            )
        else:
            asyncio.run(
                _smoke(
                    args.config,
                    args.message,
                    args.json,
                    storage_path=args.storage_path,
                    drill_name=args.drill,
                )
            )
    elif args.command == "inspect":
        from .inspect_commands import (
            _inspect_event,
            _inspect_native_ref,
            _inspect_receipts,
            _inspect_replay,
        )

        _storage_path = getattr(args, "storage_path", None)
        if args.inspect_command == "event":
            asyncio.run(
                _inspect_event(
                    args.config,
                    args.event_id,
                    storage_path=_storage_path,
                    timeline=getattr(args, "timeline", False),
                    evidence=getattr(args, "evidence", False),
                    recovery=getattr(args, "recovery", False),
                )
            )
        elif args.inspect_command == "receipts":
            asyncio.run(
                _inspect_receipts(
                    args.config,
                    event_id=args.event,
                    replay_run_id=args.replay_run,
                    storage_path=_storage_path,
                )
            )
        elif args.inspect_command == "native-ref":
            asyncio.run(
                _inspect_native_ref(
                    args.config,
                    adapter=args.adapter,
                    channel=args.channel,
                    message=args.message,
                    storage_path=_storage_path,
                )
            )
        elif args.inspect_command == "replay":
            asyncio.run(
                _inspect_replay(
                    args.config,
                    args.run_id,
                    storage_path=_storage_path,
                )
            )
    elif args.command == "evidence":
        from .evidence_commands import _evidence

        asyncio.run(
            _evidence(
                args.config,
                args.json,
                getattr(args, "event", None),
                getattr(args, "replay_run", None),
                args.include_refresh_health,
                storage_path=getattr(args, "storage_path", None),
            )
        )
    elif args.command == "trace":
        from .trace_commands import _trace_event, _trace_replay

        _storage_path = getattr(args, "storage_path", None)
        if args.trace_command == "event":
            asyncio.run(
                _trace_event(
                    args.config,
                    args.event_id,
                    args.json,
                    storage_path=_storage_path,
                )
            )
        elif args.trace_command == "replay":
            asyncio.run(
                _trace_replay(
                    args.config,
                    args.run_id,
                    args.json,
                    storage_path=_storage_path,
                )
            )
    elif args.command == "replay":
        from .replay_commands import _replay

        if getattr(args, "storage_path", None) is not None:
            print(
                "Error: --storage-path is not supported for replay. "
                "Replay requires a config file with declared routes and "
                "adapters to determine replay targets. "
                "Use --config to specify a config file.",
                file=sys.stderr,
            )
            sys.exit(EXIT_CONFIG)

        asyncio.run(
            _replay(
                args.config,
                mode=args.mode,
                event_id=args.event,
                json_output=args.json,
                target_adapters=args.target_adapters,
                route_ids=args.route_ids,
                limit=args.limit,
            )
        )
    elif args.command == "recover":
        from .recover_commands import _recover

        asyncio.run(
            _recover(
                args.config,
                event_id=args.event,
                failed_only=args.failed_only,
                since=args.since,
                dry_run=args.dry_run,
                json_output=args.json,
            )
        )
    elif args.command == "adapter":
        from .contrib import dispatch_contribution

        dispatch_contribution(args)
