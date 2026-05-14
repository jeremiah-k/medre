"""Smoke CLI command: fake bridge smoke test and drill execution."""
from __future__ import annotations

import json as _json
import logging
import sys

from .exit_codes import EXIT_CONFIG


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
    log_cfg = getattr(config, "logging", None)
    if log_cfg is None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        return

    level = getattr(log_cfg, "level", "INFO")
    fmt = getattr(log_cfg, "format", None) or "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format=fmt)


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

        if status == "PASS":
            print(f"Fake bridge smoke: PASS")
        else:
            print(f"Fake bridge smoke: FAIL")
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
            print(f"  Accounting:  inbound={acc.get('inbound_accepted', 0)} delivered={acc.get('outbound_delivered', 0)} failed={acc.get('outbound_failed', 0)}")

        # Storage info
        sp = report.get("storage_path")
        if sp:
            print(f"  Storage:     {sp}")

        # Print one limitation as a reminder.
        limitations = report.get("limitations", [])
        if limitations:
            print(f"  Note: {limitations[0]}")

    sys.exit(0 if report["status"] == "PASS" else 1)
