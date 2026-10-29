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


def _since_iso8601(value: str) -> str:
    """Argparse type for ``--since``: an aware ISO-8601 UTC timestamp.

    Naive timestamps are rejected as ambiguous — operators must state the
    offset explicitly (``2026-09-18T00:00:00+00:00`` or a trailing ``Z``).
    Returns the normalized UTC form (``+00:00`` suffix) used for the
    storage-layer TEXT comparison.
    """
    from datetime import datetime, timezone

    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a valid ISO-8601 timestamp: {value!r} "
            "(example: 2026-09-18T00:00:00+00:00)"
        ) from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            f"timestamp must include an explicit UTC offset, got {value!r} "
            "(naive timestamps are ambiguous; append +00:00 or Z)"
        )
    return parsed.astimezone(timezone.utc).isoformat()


def _recovery_page_limit(value: str) -> int:
    """Argparse type for ``--limit``: 1..MAX_RECOVERY_PAGE_LIMIT."""
    from medre.core.storage.backend import MAX_RECOVERY_PAGE_LIMIT

    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an integer, got {value!r}"
        ) from None
    if not 1 <= limit <= MAX_RECOVERY_PAGE_LIMIT:
        raise argparse.ArgumentTypeError(
            f"must be between 1 and {MAX_RECOVERY_PAGE_LIMIT}, got {limit}"
        )
    return limit


def _build_parser(*, include_adapter_cli: bool = True) -> argparse.ArgumentParser:
    from medre.core.storage.backend import (
        DEFAULT_RECOVERY_PAGE_LIMIT,
        MAX_RECOVERY_PAGE_LIMIT,
    )

    parser = argparse.ArgumentParser(
        prog="medre",
        description="Modular Event-driven Routing Engine",
        epilog="Try 'medre help routes diagram' or 'medre config use PATH' to get started.",
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
    select_p = config_sub.add_parser(
        "use", help="Remember the default bridge config for future commands"
    )
    select_p.add_argument("path", help="Existing bridge YAML to select")
    config_sub.add_parser("current", help="Show the effective default bridge config")
    config_sub.add_parser("unset", help="Forget the selected default bridge config")
    config_sub.add_parser("sample", help="Print sample config")
    bridge_p = config_sub.add_parser(
        "bridge", help="Guide bridge configuration and preview routes offline"
    )
    bridge_p.add_argument(
        "--config",
        default=None,
        help="Existing YAML; omit for installed providers' offline samples",
    )
    bridge_p.add_argument(
        "--output", required=True, help="New .yaml/.yml path; never overwritten"
    )
    bridge_p.add_argument("--source", default=None, help="Source adapter instance ID")
    bridge_p.add_argument(
        "--dest", default=None, help="Destination adapter instance ID"
    )
    bridge_p.add_argument(
        "--route-id", default="bridge", help="New route ID (default: bridge)"
    )
    bridge_p.add_argument(
        "--map",
        dest="contexts",
        nargs=2,
        action="append",
        metavar=("SOURCE_CONTEXT", "DEST_CONTEXT"),
        help="Conversation mapping; repeat for more contexts",
    )
    bridge_p.add_argument(
        "--direction",
        choices=("source_to_dest", "dest_to_source", "bidirectional"),
        default=None,
        help="Message flow relative to source and destination",
    )
    bridge_p.add_argument(
        "--source-label", default=None, help="Source origin label; empty suppresses it"
    )
    bridge_p.add_argument(
        "--dest-label", default=None, help="Reverse origin label; empty suppresses it"
    )
    bridge_p.add_argument(
        "--replace-route",
        action="store_true",
        help="Replace an existing guided text route by --route-id in a NEW output file",
    )
    bridge_p.add_argument(
        "--preview", action="store_true", help="Validate and preview without writing"
    )
    bridge_p.add_argument(
        "--json", action="store_true", help="Print the effective route plan as JSON"
    )

    native_p = config_sub.add_parser(
        "native", help="Guide an offline, provider-owned native connection draft"
    )
    native_p.add_argument("--transport", required=True, help="Installed provider type")
    native_p.add_argument("--adapter-id", required=True, help="New adapter instance ID")
    native_p.add_argument(
        "--config", default=None, help="Existing configuration to extend"
    )
    native_p.add_argument("--output", required=True, help="New private YAML path")

    # paths
    sub.add_parser("paths", help="Print resolved MEDRE paths")

    # version
    sub.add_parser("version", help="Print MEDRE version")

    dev_p = sub.add_parser(
        "dev",
        help="Synchronize or test a source checkout without sibling path assumptions",
    )
    dev_sub = dev_p.add_subparsers(dest="dev_command", required=True)
    for action in ("sync", "test"):
        dev_action = dev_sub.add_parser(
            action,
            help=(
                "Sync locked core and selected adapters"
                if action == "sync"
                else "Run an editable adapter's own pytest suite"
            ),
        )
        dev_action.add_argument(
            "--project",
            help="MEDRE checkout (default: discover from working directory)",
        )
        dev_action.add_argument(
            "--profile",
            help="Local YAML preferences (default: checkout .medre-dev.yaml)",
        )
        dev_action.add_argument(
            "--sources", help="Adapter source catalog; overrides the CI snapshot"
        )
        dev_action.add_argument(
            "--editable",
            action="append",
            default=[],
            metavar="ALIAS=PATH",
            help="Use an adapter checkout at any location",
        )
        dev_action.add_argument(
            "--sdk",
            action=argparse.BooleanOptionalAction,
            default=None,
            help="Install native SDKs (default: SDK-free)",
        )
        dev_action.add_argument(
            "--dry-run",
            action="store_true",
            help="Print commands without changing packages",
        )
        if action == "sync":
            dev_action.add_argument(
                "adapters",
                nargs="*",
                help="Source aliases, optionally with extras; default: profile or compatibility snapshot",
            )
            dev_action.add_argument("--python", help="Python request passed to uv sync")
        else:
            dev_action.add_argument(
                "--evidence-dir",
                metavar="PATH",
                help="Record the selected adapter's Git commit, diff, status and path before testing",
            )
            dev_action.add_argument("adapter", help="Editable adapter source alias")
            dev_action.add_argument(
                "pytest_args",
                nargs=argparse.REMAINDER,
                help="Arguments after -- go to pytest in the adapter checkout",
            )

    init_p = sub.add_parser(
        "init",
        help="Create an editable adapter source catalog without installing packages",
    )
    init_p.add_argument(
        "--sources", default=None, help="Catalog path (default: MEDRE config directory)"
    )

    # upgrade (update is the same command)
    update_p = sub.add_parser(
        "upgrade",
        aliases=["update"],
        help="Update MEDRE and adapters through the owning uv tool installation",
    )
    update_p.add_argument(
        "packages",
        nargs="*",
        help="Installed adapter aliases or distribution names; omit to update everything",
    )
    scope = update_p.add_mutually_exclusive_group()
    scope.add_argument(
        "--core-only", action="store_true", help="Request an upgrade of core only"
    )
    scope.add_argument(
        "--adapters-only",
        action="store_true",
        help="Request upgrades of installed adapters only",
    )
    update_p.add_argument(
        "--sources",
        metavar="PATH",
        help="Source catalog for selected aliases or --sync-sources",
    )
    update_p.add_argument(
        "--sync-sources",
        action="store_true",
        help="Apply configured sources to selected installed packages",
    )
    update_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the uv command without changing packages",
    )

    # adapters
    adapters_p = sub.add_parser(
        "adapters", help="Inspect, install, or remove adapter packages"
    )
    adapters_p.add_argument(
        "action",
        nargs="?",
        choices=("check", "sources", "install", "remove", "upgrade", "update", "reset"),
        default=None,
    )
    adapters_p.add_argument(
        "packages",
        nargs="*",
        help="Source aliases, optionally with extras (matrix[e2e])",
    )
    adapters_p.add_argument("--config", default=None, help="Path to bridge YAML config")
    adapters_p.add_argument(
        "--sources",
        default=None,
        metavar="PATH",
        help="Adapter source YAML (default: MEDRE config directory/sources.yaml)",
    )
    adapters_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the uv command without changing packages",
    )
    adapters_p.add_argument(
        "--sync-sources",
        action="store_true",
        help="Apply configured sources during adapter upgrades",
    )

    adapters_p.add_argument(
        "--include-core",
        action="store_true",
        help="With reset, also restore core to main while retaining its extras",
    )

    # diagnostics
    diag_p = sub.add_parser(
        "diagnostics",
        help="Print pre-flight diagnostics; default snapshot is read-only (requires --config or discoverable default), --refresh-health starts live runtime",
    )
    diag_p.add_argument("--config", default=None, help="Path to config file")
    diag_p.add_argument(
        "--format",
        choices=("json", "prometheus"),
        default="json",
        help="Diagnostics output format (default: json)",
    )
    diag_p.add_argument(
        "--refresh-health",
        action="store_true",
        default=False,
        help="Start live runtime, refresh adapter health once, print snapshot (may send messages)",
    )

    # routes (with sub-subcommands)
    routes_p = sub.add_parser("routes", help="Route inspection commands")
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
    routes_plan_p = routes_sub.add_parser(
        "plan",
        help="Expand routes offline and show per-leg plan, origin-label "
        "provenance, and loops (no adapter I/O)",
    )
    routes_plan_p.add_argument("--config", default=None, help="Path to config file")
    routes_plan_p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit the plan as a normalized JSON report",
    )

    routes_diagram_p = routes_sub.add_parser(
        "diagram",
        help="Preview configured route graph or per-adapter route summary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  medre routes diagram bridge.yaml          # draw in terminal\n"
            "  medre routes diagram --format text        # per-adapter detail\n"
            "  medre routes diagram --ascii              # plain ASCII graph\n"
            "  medre routes diagram --format mermaid --output bridge.mmd\n"
            "  medre routes diagram --format svg --output bridge.svg\n"
            "  medre routes diagram --view adapters --format png --output adapters.png\n"
            "  medre config use bridge.yaml  # remember default (no config copied)\n"
            "Graph images require optional local Graphviz ('dot'). No adapters start."
        ),
    )
    routes_diagram_p.add_argument(
        "config_path", nargs="?", help="Bridge YAML (shorthand for --config)"
    )
    routes_diagram_p.add_argument("--config", default=None, help="Path to config file")
    routes_diagram_p.add_argument(
        "--format",
        choices=("terminal", "text", "mermaid", "json", "dot", "svg", "png", "pdf"),
        default="terminal",
        help="Terminal graph (default), per-adapter text, graph source, or image",
    )
    routes_diagram_p.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Save output to a NEW private file; required for svg/png/pdf",
    )
    routes_diagram_p.add_argument(
        "--view",
        choices=("contexts", "adapters"),
        default="contexts",
        help="Group by endpoint contexts (default) or adapters (terminal/Mermaid/DOT/images)",
    )
    routes_diagram_p.add_argument(
        "--ascii",
        action="store_true",
        default=False,
        help="Use ASCII instead of Unicode box-drawing (terminal format only)",
    )
    routes_diagram_p.add_argument(
        "--full-context",
        action="store_true",
        default=False,
        help="Include complete room/address values; may disclose private topology",
    )

    routes_explain_p = routes_sub.add_parser(
        "explain", help="Explain offline source matching without delivery guarantees"
    )
    routes_explain_p.add_argument("--config", default=None, help="Path to config file")
    routes_explain_p.add_argument("--source-adapter", required=True, help="Instance ID")
    routes_explain_p.add_argument(
        "--source-context", required=True, help="Exact conversation ID"
    )
    routes_explain_p.add_argument("--event-kind", default="message.text")
    routes_explain_p.add_argument(
        "--sender-id", default=None, help="Sender ID for policy checks"
    )
    routes_explain_p.add_argument("--json", action="store_true")

    # smoke
    smoke_p = sub.add_parser(
        "smoke",
        help="Local validation tooling, not daily operation (developers/CI)",
    )
    smoke_p.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: examples/configs/fake-bridge-smoke.yaml)",
    )
    smoke_p.add_argument(
        "--message",
        default=None,
        help="Text for the injected event (normal smoke only)",
    )
    smoke_p.add_argument(
        "--source-adapter",
        default=None,
        metavar="ID",
        help="Inject from this route source (default: first eligible source; normal smoke only)",
    )
    smoke_p.add_argument(
        "--source-context",
        default=None,
        metavar="CONTEXT",
        help="Inject from this conversation address (normal smoke only)",
    )
    smoke_modes = smoke_p.add_mutually_exclusive_group()
    smoke_modes.add_argument(
        "--drill",
        default=None,
        metavar="NAME",
        help="Run named failure drill instead of normal smoke",
    )
    smoke_modes.add_argument(
        "--run-session",
        action="store_true",
        default=False,
        help="Run a complete bridge session (starts runtime, may send messages): start→inject→poll→stop→snapshot→report",
    )
    smoke_p.add_argument(
        "--snapshot-dir",
        default=None,
        metavar="DIR",
        help="Directory for final snapshot JSON (run-session only; default: storage directory)",
    )
    smoke_p.add_argument(
        "--scenario",
        default=None,
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
        "--storage-path",
        default=None,
        metavar="PATH",
        help="Override config storage to SQLite at PATH for persisting smoke evidence",
    )
    smoke_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON report"
    )

    # evidence
    evidence_p = sub.add_parser(
        "evidence",
        help="Specialized storage-backed report, usually inspect event --evidence (read-only; requires --storage-path)",
    )
    evidence_p.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
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

    # inspect (with sub-subcommands)
    inspect_p = sub.add_parser(
        "inspect",
        help="Primary read-only investigation command (requires --storage-path)",
    )
    inspect_sub = inspect_p.add_subparsers(dest="inspect_command", required=True)

    inspect_content = inspect_sub.add_parser(
        "content", help="Count retained core content without displaying messages"
    )
    inspect_content.add_argument(
        "--event",
        default=None,
        metavar="EVENT_ID",
        help="Read-only dependency preview for one canonical event (no content bytes)",
    )
    inspect_content.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )

    inspect_decisions = inspect_sub.add_parser(
        "decisions",
        help="Read immutable operator cancellation evidence for one event",
    )
    inspect_decisions.add_argument("--event", required=True, metavar="EVENT_ID")
    inspect_decisions.add_argument("--storage-path", required=True, metavar="PATH")

    inspect_radio = inspect_sub.add_parser(
        "radio-attempt",
        help="Inspect exact radio packet progress without granting sends",
    )
    inspect_radio.add_argument("outbox_id", help="Exact durable outbox ID")
    inspect_radio.add_argument("attempt", type=int, help="Positive attempt generation")
    inspect_radio.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    inspect_radio.add_argument(
        "--assess-continuation",
        action="store_true",
        default=False,
        help="Explain retained tail evidence without creating or claiming work",
    )

    # inspect event <event_id>
    inspect_evt = inspect_sub.add_parser("event", help="Inspect a canonical event")
    inspect_evt.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
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
    inspect_rcpt.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
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
    inspect_nref.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    inspect_nref.add_argument("--adapter", required=True, help="Adapter name")
    inspect_nref.add_argument(
        "--channel",
        default=None,
        help="Native channel ID (omit for channelless protocols)",
    )
    inspect_nref.add_argument("--message", required=True, help="Native message ID")

    # inspect pressure [--source SRC]
    inspect_pressure = inspect_sub.add_parser(
        "pressure",
        help="List pre-admission pressure aggregates (counters only, read-only)",
    )
    inspect_pressure.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    inspect_pressure.add_argument(
        "--source",
        default=None,
        help="Filter by adapter source id",
    )
    inspect_pressure.add_argument(
        "--limit",
        type=_recovery_page_limit,
        default=None,
        metavar="N",
        help="Return only the newest N aggregate rows (1..MAX)",
    )

    # inspect replay <run_id>
    inspect_rpl = inspect_sub.add_parser(
        "replay", help="Inspect a replay run timeline (read-only)"
    )
    inspect_rpl.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    inspect_rpl.add_argument("run_id", help="Replay run ID to inspect")

    # trace (with sub-subcommands)
    trace_p = sub.add_parser(
        "trace",
        help="Specialized timeline, usually inspect event --timeline (read-only; requires --storage-path)",
    )
    trace_sub = trace_p.add_subparsers(dest="trace_command", required=True)

    # trace event <event_id>
    trace_evt = trace_sub.add_parser(
        "event", help="Assemble timeline for a canonical event"
    )
    trace_evt.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    trace_evt.add_argument("event_id", help="Canonical event ID to trace")
    trace_evt.add_argument(
        "--json", action="store_true", default=False, help="Output JSON timeline"
    )

    # trace replay <run_id>
    trace_rpl = trace_sub.add_parser(
        "replay", help="Assemble timeline for a replay run"
    )
    trace_rpl.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
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
        "--run-id",
        default=None,
        metavar="RUN_ID",
        help="Operator-assigned identifier for this replay execution",
    )

    # recover
    recover_p = sub.add_parser(
        "recover",
        help="Specialized recovery classification, usually inspect event --recovery (read-only; requires --storage-path)",
    )
    recover_p.add_argument(
        "--storage-path",
        required=True,
        metavar="PATH",
        help="Path to SQLite database (read-only)",
    )
    recover_p.add_argument(
        "--event", default=None, metavar="EVENT_ID", help="Event ID to analyze"
    )
    recover_p.add_argument(
        "--since",
        default=None,
        metavar="TIMESTAMP",
        type=_since_iso8601,
        help=(
            "Only deliveries whose canonical event timestamp is >= TIMESTAMP "
            "(inclusive). ISO-8601 with an explicit UTC offset, e.g. "
            "2026-09-18T00:00:00+00:00; naive timestamps are rejected"
        ),
    )
    recover_p.add_argument(
        "--limit",
        default=None,
        type=_recovery_page_limit,
        metavar="N",
        help=(
            f"Max unresolved deliveries per scan page "
            f"(default {DEFAULT_RECOVERY_PAGE_LIMIT}, max "
            f"{MAX_RECOVERY_PAGE_LIMIT})"
        ),
    )
    recover_p.add_argument(
        "--cursor",
        default=None,
        metavar="TOKEN",
        help=(
            "Continuation token from a previous scan page's next_cursor "
            "(malformed or version-mismatched tokens are rejected)"
        ),
    )
    recover_p.add_argument(
        "--json", action="store_true", default=False, help="Output JSON runbook"
    )

    recover_p.add_argument(
        "--exact-plan-id",
        default=None,
        metavar="PLAN",
        help="Read-only exact delivery-plan selection (requires --event)",
    )
    recover_p.add_argument(
        "--exact-adapter",
        default=None,
        metavar="ADAPTER",
        help="Read-only exact target adapter (requires --exact-plan-id)",
    )
    channel_group = recover_p.add_mutually_exclusive_group()
    channel_group.add_argument(
        "--exact-channel",
        default=None,
        metavar="CHANNEL",
        help="Select one exact nonempty channel",
    )
    channel_group.add_argument(
        "--exact-no-channel",
        action="store_true",
        help="Select the delivery identity with no channel",
    )

    # storage (with sub-subcommands)
    storage_p = sub.add_parser("storage", help="Storage management commands")
    storage_sub = storage_p.add_subparsers(dest="storage_command", required=True)

    storage_status_p = storage_sub.add_parser(
        "status", help="Report storage schema health (read-only)"
    )
    storage_status_p.add_argument(
        "--storage-path",
        required=False,
        default=None,
        metavar="PATH",
        help="Path to SQLite database (default: standard database path)",
    )

    storage_reset_p = storage_sub.add_parser(
        "reset", help="Delete storage database (destructive)"
    )
    storage_reset_p.add_argument(
        "--storage-path",
        required=False,
        default=None,
        metavar="PATH",
        help="Path to SQLite database to delete (default: standard database path)",
    )
    storage_reset_p.add_argument(
        "--backup",
        action="store_true",
        default=False,
        help="Copy database to .bak-<timestamp> before deleting",
    )
    storage_reset_p.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Confirm destructive reset",
    )

    # Adapter/plugin contributed commands (adapter, plugin namespaces)
    parser.set_defaults(_adapter_cli_failures=())
    if include_adapter_cli:
        from .contrib import register_adapter_contributors

        parser.set_defaults(_adapter_cli_failures=register_adapter_contributors(sub))

    # support (with sub-subcommands)
    support_p = sub.add_parser(
        "support",
        help="Operator support bundle commands (offline, redacted)",
    )
    support_sub = support_p.add_subparsers(dest="support_command", required=True)
    support_bundle_p = support_sub.add_parser(
        "bundle",
        help="Write a redacted support bundle ZIP (offline; no adapter I/O)",
    )
    support_bundle_p.add_argument(
        "--config",
        default=None,
        help="Path to config file (default: standard discovery)",
    )
    support_bundle_p.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help="Output ZIP path (default: medre-support-bundle.zip in cwd)",
    )

    from .help_navigation import describe_defaults

    describe_defaults(parser)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate command handler."""
    from .help_navigation import group_help, help_request, show_command_help

    arguments, help_only = help_request(
        list(argv) if argv is not None else sys.argv[1:]
    )
    package_actions = {"sources", "install", "remove", "upgrade", "update", "reset"}
    adapter_action = None
    if arguments and arguments[0] == "adapters":
        tail = iter(arguments[1:])
        for value in tail:
            if value in {"--sources", "--config"}:
                next(tail, None)
            elif not value.startswith("-"):
                adapter_action = value
                break
    managing_packages = bool(arguments) and (
        arguments[0] in {"init", "upgrade", "update"}
        or (arguments[0] == "adapters" and adapter_action in package_actions)
    )
    # Updating or removing a broken provider must not import that provider.
    parser = _build_parser(include_adapter_cli=not managing_packages)
    if help_only:
        show_command_help(parser, arguments)
    args = parser.parse_args(group_help(parser, arguments))

    if (
        args.command == "inspect"
        and args.inspect_command == "radio-attempt"
        and args.attempt < 1
    ):
        parser.error("radio-attempt requires a positive attempt generation")

    if args.command in ("upgrade", "update"):
        if args.core_only and args.packages:
            parser.error("--core-only cannot select adapters")
        if args.sources and not (args.packages or args.sync_sources):
            parser.error("--sources requires selected adapters or --sync-sources")

    if args.command == "adapters":
        managing = args.action in ("install", "remove")
        upgrading = args.action in ("upgrade", "update")
        resetting = args.action == "reset"
        if args.include_core and not resetting:
            parser.error("--include-core requires adapters reset")
        if (managing and not args.packages) or (
            args.packages and not (managing or upgrading or resetting)
        ):
            parser.error(
                "adapters install/remove require a package selection; only install, remove, upgrade, and reset accept selections"
            )
        if args.dry_run and not (managing or upgrading or resetting):
            parser.error(
                "--dry-run requires adapters install, remove, upgrade, or reset"
            )
        if args.sync_sources and not upgrading:
            parser.error("--sync-sources requires adapters upgrade")
        if upgrading and args.sources and not (args.packages or args.sync_sources):
            parser.error("--sources requires selected adapters or --sync-sources")
        if args.sources and args.action not in (
            "sources",
            "install",
            "remove",
            "upgrade",
            "update",
        ):
            parser.error("--sources requires an adapter package-management action")
        if args.config and args.action not in (None, "check"):
            parser.error(
                "--config is for adapter inspection; use --sources for installation sources"
            )

    if args.command == "dev":
        from .dev_commands import _development

        _development(args)
    elif args.command in ("init", "update", "upgrade") or (
        args.command == "adapters" and args.action in package_actions
    ):
        from .install_commands import _manage_installation

        _manage_installation(args)
    elif args.command == "run":
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
        elif args.config_command == "bridge":
            from .bridge_commands import _config_bridge

            _config_bridge(args)
        elif args.config_command == "native":
            from .native_commands import _config_native

            _config_native(args)
        elif args.config_command in {"use", "current", "unset"}:
            from .config_commands import _config_selection

            _config_selection(args.config_command, getattr(args, "path", None))
    elif args.command == "paths":
        from .config_commands import _paths

        _paths()
    elif args.command == "version":
        _version()
    elif args.command == "adapters":
        from .config_commands import _adapters, _adapters_check

        if args.action == "check":
            _adapters_check(args.config, cli_failures=args._adapter_cli_failures)
        else:
            _adapters(cli_failures=args._adapter_cli_failures)
    elif args.command == "diagnostics":
        from .diagnostics_commands import _diagnostics, _diagnostics_refresh

        if getattr(args, "refresh_health", False):
            asyncio.run(_diagnostics_refresh(args.config, output_format=args.format))
        else:
            _diagnostics(args.config, output_format=args.format)
    elif args.command == "routes":
        from .route_commands import (
            _routes_diagram,
            _routes_explain,
            _routes_list,
            _routes_plan,
            _routes_topology,
            _routes_validate,
        )

        if args.routes_command == "validate":
            _routes_validate(args.config)
        elif args.routes_command == "topology":
            _routes_topology(args.config)
        elif args.routes_command == "list":
            _routes_list(args.config)
        elif args.routes_command == "plan":
            _routes_plan(args.config, as_json=args.json)
        elif args.routes_command == "diagram":
            if args.config and args.config_path:
                parser.error(
                    "routes diagram: choose either positional CONFIG or --config"
                )
            _routes_diagram(
                args.config or args.config_path,
                output_format=args.format,
                view=args.view,
                full_context=args.full_context,
                output_path=args.output,
                ascii_only=args.ascii,
            )
        elif args.routes_command == "explain":
            _routes_explain(
                args.config,
                args.source_adapter,
                args.source_context,
                args.event_kind,
                sender_id=args.sender_id,
                as_json=args.json,
            )
    elif args.command == "smoke":
        from .smoke_commands import _run_session, _smoke

        normal_smoke = args.drill is None and not args.run_session
        if args.message is not None and not normal_smoke:
            parser.error("--message is supported only for normal smoke")
        if args.snapshot_dir is not None and not args.run_session:
            parser.error("--snapshot-dir requires --run-session")
        if args.scenario is not None and not args.run_session:
            parser.error("--scenario requires --run-session")
        if args.source_adapter is not None and not normal_smoke:
            parser.error("--source-adapter is supported only for normal smoke")
        if args.source_context is not None and not normal_smoke:
            parser.error("--source-context is supported only for normal smoke")

        if getattr(args, "run_session", False):
            asyncio.run(
                _run_session(
                    args.config,
                    snapshot_dir=getattr(args, "snapshot_dir", None),
                    json_output=args.json,
                    scenario=(
                        args.scenario if args.scenario is not None else "happy_path"
                    ),
                    storage_path=getattr(args, "storage_path", None),
                )
            )
        else:
            asyncio.run(
                _smoke(
                    args.config,
                    args.message if args.message is not None else "medre smoke test",
                    args.json,
                    drill_name=args.drill,
                    storage_path=getattr(args, "storage_path", None),
                    source_adapter=args.source_adapter,
                    source_context=args.source_context,
                )
            )
    elif args.command == "inspect":
        from .inspect_commands import (
            _inspect_content,
            _inspect_event,
            _inspect_native_ref,
            _inspect_pressure,
            _inspect_radio_attempt,
            _inspect_receipts,
            _inspect_replay,
        )

        if args.inspect_command == "decisions":
            from .decision_inspect import _inspect_decisions

            asyncio.run(_inspect_decisions(args.event, storage_path=args.storage_path))
        elif args.inspect_command == "content":
            asyncio.run(
                _inspect_content(storage_path=args.storage_path, event_id=args.event)
            )
        elif args.inspect_command == "radio-attempt":
            asyncio.run(
                _inspect_radio_attempt(
                    args.outbox_id,
                    args.attempt,
                    storage_path=args.storage_path,
                    assess_continuation=args.assess_continuation,
                )
            )
        elif args.inspect_command == "event":
            asyncio.run(
                _inspect_event(
                    args.event_id,
                    storage_path=args.storage_path,
                    timeline=getattr(args, "timeline", False),
                    evidence=getattr(args, "evidence", False),
                    recovery=getattr(args, "recovery", False),
                )
            )
        elif args.inspect_command == "receipts":
            asyncio.run(
                _inspect_receipts(
                    event_id=args.event,
                    replay_run_id=args.replay_run,
                    storage_path=args.storage_path,
                )
            )
        elif args.inspect_command == "native-ref":
            asyncio.run(
                _inspect_native_ref(
                    adapter=args.adapter,
                    channel=args.channel,
                    message=args.message,
                    storage_path=args.storage_path,
                )
            )
        elif args.inspect_command == "pressure":
            asyncio.run(
                _inspect_pressure(
                    storage_path=args.storage_path,
                    source=args.source,
                    limit=args.limit,
                )
            )
        elif args.inspect_command == "replay":
            asyncio.run(
                _inspect_replay(
                    args.run_id,
                    storage_path=args.storage_path,
                )
            )
    elif args.command == "evidence":
        from .evidence_commands import _evidence

        asyncio.run(
            _evidence(
                args.json,
                getattr(args, "event", None),
                getattr(args, "replay_run", None),
                storage_path=args.storage_path,
            )
        )
    elif args.command == "trace":
        from .trace_commands import _trace_event, _trace_replay

        if args.trace_command == "event":
            asyncio.run(
                _trace_event(
                    args.event_id,
                    args.json,
                    storage_path=args.storage_path,
                )
            )
        elif args.trace_command == "replay":
            asyncio.run(
                _trace_replay(
                    args.run_id,
                    args.json,
                    storage_path=args.storage_path,
                )
            )
    elif args.command == "replay":
        from .replay_commands import _replay

        asyncio.run(
            _replay(
                config_path=args.config,
                mode=args.mode,
                event_id=args.event,
                json_output=args.json,
                target_adapters=args.target_adapters,
                route_ids=args.route_ids,
                limit=args.limit,
                run_id=args.run_id or "",
            )
        )
    elif args.command == "recover":
        from .recover_commands import _recover

        asyncio.run(
            _recover(
                event_id=args.event,
                since=args.since,
                limit=args.limit,
                cursor=args.cursor,
                json_output=args.json,
                storage_path=args.storage_path,
                exact_plan_id=args.exact_plan_id,
                exact_adapter=args.exact_adapter,
                exact_channel=args.exact_channel,
                exact_no_channel=args.exact_no_channel,
            )
        )
    elif args.command == "storage":
        from medre.config.paths import resolve as resolve_paths

        from .storage_commands import _storage_reset, _storage_status

        if args.storage_command == "status":
            path = args.storage_path or str(resolve_paths().database_path)
            asyncio.run(_storage_status(path))
        elif args.storage_command == "reset":
            path = args.storage_path or str(resolve_paths().database_path)
            asyncio.run(
                _storage_reset(
                    path,
                    backup=args.backup,
                    yes=args.yes,
                )
            )
    elif args.command == "adapter":
        from .contrib import dispatch_contribution

        dispatch_contribution(args)
    elif args.command == "support":
        from .diagnostics_commands import _support_bundle

        if args.support_command == "bundle":
            _support_bundle(args.config, args.output)
