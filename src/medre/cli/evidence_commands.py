"""Evidence CLI command: collect evidence bundles for support (read-only; requires --storage-path)."""

from __future__ import annotations

import json as _json
import sys

from medre.runtime.evidence._bundle import collect_evidence_bundle

from .exit_codes import EXIT_CONFIG


async def _evidence(
    json_output: bool,
    event_id: str | None,
    replay_run_id: str | None,
    *,
    storage_path: str,
) -> None:
    """Collect and print an evidence bundle.

    Read-only derived view: delegates to ``collect_evidence_bundle`` which
    opens storage in read-only mode and queries persisted rows.  All derived
    sections are computed on demand — no storage mutation occurs.
    """
    report = await collect_evidence_bundle(
        None,
        event_id=event_id,
        replay_run_id=replay_run_id,
        include_refresh_health=False,
        storage_path=storage_path,
    )

    if json_output:
        print(_json.dumps(report, sort_keys=True, indent=2))
    else:
        # Human-readable summary.
        status = report["status"]
        if status == "passed":
            print("Evidence: PASSED")
        elif status == "partial":
            print("Evidence: PARTIAL (some sections incomplete)")
        else:
            print("Evidence: ERROR")

        print(f"  Collected at:  {report['collected_at']}")
        print(f"  Version:       {report['medre_version']}")
        print(f"  Config source: {report.get('config_source', 'N/A')}")
        print(f"  Runtime started: {report['runtime_started']}")

        sections = report.get("sections", {})
        for name, section in sorted(sections.items()):
            sec_status = section.get("status", "unknown")
            marker = {
                "passed": "\u2713",
                "partial": "\u26a0",
                "error": "\u2717",
                "skipped": "-",
            }.get(sec_status, "?")
            print(f"  {marker} {name}: {sec_status}")
            if section.get("error"):
                print(f"      {section['error']}")

        errors = report.get("errors", [])
        if errors:
            print()
            print(f"Errors ({len(errors)}):")
            for err in errors:
                print(f"  \u2717 {err}")

    # Exit 0 for passed/partial, EXIT_CONFIG for config load failure.
    if report["status"] == "error":
        sys.exit(EXIT_CONFIG)
