"""Read-only offline readiness report command.

The report composes the compatibility snapshot, installed-distribution
metadata, config validation, and pure provider-owned preflight into one
authoritative offline view. Nothing is contacted, started, or written;
see :mod:`medre.runtime.readiness_report` for the observation boundary.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .exit_codes import EXIT_FAILED
from .json import to_json


def _provider_line(provider: dict[str, Any]) -> str:
    transport = str(provider["transport"])
    distribution = str(provider["distribution"])
    ref = provider.get("ref")
    revision = str(ref)[:8] if isinstance(ref, str) else "unknown"
    parts = [
        f"  {transport:<14} {distribution} @ {revision}",
        str(provider.get("distribution_status", "unknown")),
    ]
    registration = provider.get("registration")
    if registration is not None and registration != "registered":
        parts.append(f"({registration})")
    return "  ".join(parts)


def _config_line(config: dict[str, Any]) -> str:
    source = config.get("source")
    source_text = f"source {source}" if isinstance(source, str) else "no source"
    detail = (
        f"{config.get('adapter_count', 0)} adapter(s), "
        f"{config.get('enabled_adapter_count', 0)} enabled"
    )
    status = str(config.get("status", "unknown"))
    if status == "valid":
        return f"Configuration: valid ({source_text}; {detail})"
    category = config.get("error_category") or status
    section = config.get("section_path")
    where = f" at {section}" if isinstance(section, str) else ""
    return f"Configuration: {status} ({source_text}){where} [{category}]"


def _adapter_line(row: dict[str, Any]) -> str:
    header = (
        f"  {row.get('adapter_id', '?')} ({row.get('adapter_kind', 'real')}, "
        f"{'enabled' if row.get('enabled') else 'disabled'})"
    )
    facts: list[str] = []
    sdk_status = row.get("sdk_distribution_status")
    if sdk_status == "missing":
        facts.append(f"sdk distribution {row.get('sdk_distribution')} missing")
    elif sdk_status == "unknown":
        facts.append("sdk requirement unknown")
    provider_status = row.get("provider_distribution_status")
    if provider_status == "missing":
        facts.append(
            f"provider distribution {row.get('provider_distribution')} missing"
        )
    if row.get("config") == "actionable_error":
        facts.append(f"config error [{row.get('blocked_category')}]")
    preflight = str(row.get("preflight", "unknown"))
    if preflight == "blocked":
        facts.append(f"preflight blocked [{row.get('blocked_category')}]")
    if not facts:
        facts.append(f"preflight {preflight}")
    return f"{header}: " + "; ".join(facts)


def _routes_line(routes: dict[str, Any] | None) -> str:
    if routes is None:
        return "Routes: not evaluated (configuration not loaded)"
    status = str(routes.get("status", "unknown"))
    detail = f"{routes.get('total', 0)} configured ({routes.get('enabled', 0)} enabled)"
    if status == "passed":
        return f"Routes: plan passed ({detail})"
    return f"Routes: blocked [{routes.get('error_category')}] ({detail})"


def _render_human(report: dict[str, Any]) -> None:
    """Print the short, actionable, path-free operator view."""
    print("Offline readiness report")
    manifest = report.get("manifest", {})
    if isinstance(manifest, dict) and manifest.get("status") == "available":
        print(
            f"Compatibility snapshot: {manifest.get('provider_count')} provider(s), "
            f"{manifest.get('installed_count')} installed"
        )
        providers = manifest.get("providers", [])
        if isinstance(providers, list):
            for provider in providers:
                if isinstance(provider, dict):
                    print(_provider_line(provider))
    else:
        status = (
            manifest.get("status", "unknown")
            if isinstance(manifest, dict)
            else "unknown"
        )
        print(f"Compatibility snapshot: unavailable [{status}]")
    config = report.get("config", {})
    if not isinstance(config, dict):
        config = {}
    print(_config_line(config))
    adapters = report.get("adapters", [])
    if isinstance(adapters, list) and adapters:
        print("Adapters:")
        for row in adapters:
            if isinstance(row, dict):
                print(_adapter_line(row))
    routes = config.get("routes")
    print(_routes_line(routes if isinstance(routes, dict) else None))
    boundary = report.get("boundary", {})
    statement = boundary.get("statement", "") if isinstance(boundary, dict) else ""
    if statement:
        print(f"Boundary: {statement}")
    guidance = report.get("guidance", [])
    if isinstance(guidance, list) and guidance:
        print("Next steps:")
        for step in guidance:
            print(f"  - {step}")


def _readiness(args: argparse.Namespace) -> None:
    """Print the offline readiness report; never contacts or writes anything."""
    from medre.runtime.readiness_report import build_readiness_report

    try:
        report = build_readiness_report(args.config)
    except Exception as exc:  # pragma: no cover - composition is defensive
        print(f"Readiness report error: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(EXIT_FAILED) from None
    if args.json:
        print(to_json(report))
    else:
        _render_human(report)
