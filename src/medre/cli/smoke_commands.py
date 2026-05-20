"""Smoke CLI command: fake bridge smoke test, drill execution, and run-session."""

from __future__ import annotations

import json as _json
import sys


def _transport_for_adapter(adapter_id: str, config: object) -> str:
    """Look up the transport type for an adapter_id from config."""
    adapters = getattr(config, "adapters", None)
    if adapters is None:
        return "unknown"
    for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
        group = getattr(adapters, transport, {})
        for _name, rtc in group.items():
            if rtc.adapter_id == adapter_id:
                return transport
    return "unknown"


def _setup_logging(config: object) -> None:
    """Apply logging configuration from the parsed config."""
    from medre.core.observability.logging import setup_logging

    log_cfg = getattr(config, "logging", None)
    if log_cfg is None:
        setup_logging(level="INFO", json_format=False)
        return

    level = getattr(log_cfg, "level", "INFO")
    fmt = getattr(log_cfg, "format", "text")
    overrides = getattr(log_cfg, "overrides", None)
    setup_logging(
        level=level,
        json_format=(fmt == "json"),
        overrides=overrides if overrides else None,
    )


async def _smoke(
    config_path: str | None,
    message_text: str,
    json_output: bool,
    storage_path: str | None = None,
    drill_name: str | None = None,
) -> None:
    """Run fake bridge smoke test and print a compact evidence report.

    Builds and starts the runtime with fake adapters, injects one
    ``message.text`` event through the full pipeline, inspects storage
    evidence, and prints a PASS/FAIL report.  Docker-free, network-free.

    Exit codes: 0 on PASS, 1 on FAIL.
    """
    if drill_name is not None:
        from medre.runtime.drill import run_drill

        report = await run_drill(
            drill_name,
            config_path=config_path,
            storage_path=storage_path,
        )
    else:
        from medre.runtime.smoke import run_fake_bridge_smoke

        report = await run_fake_bridge_smoke(
            config_path,
            message_text=message_text,
            storage_path=storage_path,
        )

    if json_output:
        print(_json.dumps(report, sort_keys=True, indent=2))
    else:
        # Human-readable summary
        status = report["status"]
        event_id = report.get("event_id", "N/A")
        source = report.get("source_adapter", "N/A")
        targets = report.get("target_adapters", [])
        routes = report.get("route_ids", [])
        acc = report.get("accounting", {})
        n_receipts = len(report.get("delivery_receipts", []))
        n_refs = len(report.get("native_refs", []))

        # JSON status uses lowercase "passed"/"failed"; terminal output uses
        # "PASS"/"FAIL" labels for visual clarity.
        if status == "passed":
            print("Fake bridge smoke: PASS")
        else:
            print("Fake bridge smoke: FAIL")
            reasons = report.get("fail_reasons", [])
            for r in reasons:
                print(f"  \u2717 {r}")

        print(f"  Event:       {event_id}")
        print(f"  Source:      {source}")
        print(f"  Targets:     {', '.join(targets) if targets else '(none)'}")
        print(f"  Routes:      {', '.join(routes) if routes else '(none)'}")
        print(f"  Receipts:    {n_receipts}")
        print(f"  Native refs: {n_refs}")
        if acc:
            print(
                f"  Accounting:  inbound={acc.get('inbound_accepted', 0)} delivered={acc.get('outbound_delivered', 0)} failed={acc.get('outbound_failed', 0)}"
            )

        # Storage info
        sp = report.get("storage_path")
        if sp:
            print(f"  Storage:     {sp}")

        # Print one limitation as a reminder.
        limitations = report.get("limitations", [])
        if limitations:
            print(f"  Note: {limitations[0]}")

    sys.exit(0 if report["status"] == "passed" else 1)


async def _run_session(
    config_path: str | None,
    storage_path: str | None,
    snapshot_dir: str | None,
    json_output: bool,
    scenario: str = "happy_path",
) -> None:
    """Run a complete bridge session and print a cross-linked evidence report.

    Starts runtime, injects one fake event, polls for delivery, stops
    gracefully, writes final snapshot, and produces a report with
    cross-linked CLI commands for further inspection.

    Exit codes: 0 on PASS, 1 on FAIL.
    """
    if storage_path is None:
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            suffix=".db",
            prefix="medre-session-",
            delete=False,
        )
        storage_path = tmp.name
        tmp.close()
        print(f"No --storage-path provided; using temporary database: {storage_path}")

    from medre.runtime.run_session.orchestration import run_bridge_session

    report = await run_bridge_session(
        config_path,
        storage_path=storage_path,
        snapshot_dir=snapshot_dir,
        scenario=scenario,
    )

    if json_output:
        print(_json.dumps(report, sort_keys=True, indent=2))
    else:
        status = report["status"]
        event_id = report.get("event_id", "N/A")
        route_id = report.get("route_id", "N/A")
        source = report.get("source_adapter", "N/A")
        targets = report.get("target_adapters", [])
        acc = report.get("accounting") or {}
        receipts = report.get("delivery_receipts", [])
        native_refs = report.get("native_refs", [])
        snap_checks = report.get("final_snapshot_checks", {})
        commands = report.get("commands", {})
        storage = report.get("storage_path", "N/A")
        snap_path = report.get("final_snapshot_path", "N/A")

        # JSON status uses lowercase "passed"/"failed"; terminal output uses
        # "PASS"/"FAIL" labels for visual clarity.
        if status == "passed":
            print("Run session: PASS")
        else:
            print("Run session: FAIL")
            for r in report.get("fail_reasons", []):
                print(f"  \u2717 {r}")

        print(f"  Event:       {event_id}")
        print(f"  Route:       {route_id}")
        print(f"  Source:      {source}")
        print(f"  Targets:     {', '.join(targets) if targets else '(none)'}")
        print(f"  Receipts:    {len(receipts)}")
        print(f"  Native refs: {len(native_refs)}")
        print(f"  Storage:     {storage}")
        print(f"  Snapshot:    {snap_path}")
        if acc:
            # Same 5 field names as run_commands.py accounting printer.
            print(
                f"  Accounting:  inbound={acc.get('inbound', 0)} "
                f"outbound_delivered={acc.get('outbound_delivered', 0)} "
                f"outbound_failed={acc.get('outbound_failed', 0)} "
                f"loop_prevented={acc.get('loop_prevented', 0)} "
                f"capacity_rejections={acc.get('capacity_rejections', 0)}"
            )
        if snap_checks:
            print(
                f"  Snapshot:    schema_version={snap_checks.get('schema_version', '?')} "
                f"runtime_state={snap_checks.get('runtime_state', '?')}"
            )
        if commands:
            print("  Commands:")
            # commands_text is nested: { primary: {...}, specialized: {...} }
            text_commands = commands.get("commands_text", commands)
            if "primary" in text_commands and isinstance(
                text_commands["primary"],
                dict,
            ):
                for label, cmd in text_commands["primary"].items():
                    print(f"    {label}: {cmd}")
                for label, cmd in text_commands.get("specialized", {}).items():
                    print(f"    {label}: {cmd}")
            else:
                # Legacy flat shape fallback.
                for label, cmd in text_commands.items():
                    print(f"    {label}: {cmd}")

    sys.exit(0 if report["status"] == "passed" else 1)
