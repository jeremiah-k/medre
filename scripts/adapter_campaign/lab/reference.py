"""Opt-in Matrix/XMPP reference journey using the native fidelity oracle.

A positive verdict requires native peers at both ends, an independently
observed runtime restart, and read-only reconciliation with the SQLite ledger.
No sample/fake observation or unexecuted Docker stage can write a pass verdict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .sources import write_json

DIRECTIONS = ("matrix", "xmpp")


async def provision(root: Path) -> None:
    """Prepare only Matrix and XMPP identities; do not provision other providers."""
    from .provision import matrix, xmpp

    accounts = json.loads((root / "config/accounts.json").read_text())
    path = root / "results/provision.json"
    results = json.loads(path.read_text()) if path.exists() else {}
    failures = []
    for name, function in (("matrix", matrix), ("xmpp", xmpp)):
        if results.get(name, {}).get("status") == "ready":
            continue
        try:
            runtime, endpoint = await function(root, accounts)
            results[name] = {
                "runtime": runtime,
                "endpoint": endpoint,
                "status": "ready",
            }
        except Exception as exc:
            results[name] = {"status": "failed", "error_type": type(exc).__name__}
            failures.append(name)
        write_json(path, results)
    if failures:
        raise RuntimeError("Reference provisioning failed: " + ", ".join(failures))


def summarize(analysis: dict[str, Any]) -> dict[str, Any]:
    """Reduce only a fully verified native analysis to content-free proof."""
    from scripts.adapter_campaign.fidelity import CASES

    expected = [name for name, _source, _parent in CASES]
    cases = analysis.get("cases")
    if (
        analysis.get("status") != "passed"
        or type(analysis.get("version")) is not int
        or analysis["version"] != 1
        or not isinstance(cases, list)
        or [row.get("name") for row in cases if isinstance(row, dict)] != expected
        or len(cases) != len(expected)
        or any(row.get("status") != "passed" for row in cases)
    ):
        raise ValueError("Reference native analysis did not establish every witness")
    counts = {
        source: sum(1 for _name, origin, _parent in CASES if origin == source)
        for source in DIRECTIONS
    }
    return {
        "version": 1,
        "status": "passed",
        "evidence_level": "docker_native_independent_peers_and_sqlite",
        "campaign": "matrix-xmpp-bidirectional-reference",
        "case_count": len(expected),
        "source_case_counts": counts,
        "requires": [
            "independent_source_and_recipient_peers",
            "native_receipt_and_durable_ledger_reconciliation",
            "two_ordered_runtime_generations",
            "complete_reply_and_fallback_cases",
        ],
        "limits": [
            "Disposable Docker Synapse/Prosody, not production accounts",
            "Plaintext Matrix room, no E2EE peer-trust qualification",
            "No claim about hardware radios or unrelated provider identities",
        ],
    }


def source_revisions(root: Path) -> dict[str, dict[str, str | bool]]:
    """Bind the verdict to the actual, pathless build snapshots."""
    inventory = json.loads((root / "sources.json").read_text())
    if not isinstance(inventory, dict):
        raise ValueError("Reference source inventory is invalid")
    result: dict[str, dict[str, str | bool]] = {}
    for name in ("medre", "matrix", "xmpp"):
        record = inventory.get(name)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", record["commit"])
            or not isinstance(record.get("snapshot_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["snapshot_sha256"])
            or type(record.get("dirty")) is not bool
        ):
            raise ValueError("Reference source snapshot is incomplete")
        result[name] = {
            "commit": record["commit"],
            "snapshot_sha256": record["snapshot_sha256"],
            "dirty": record["dirty"],
        }
    return result


async def run(root: Path) -> None:
    """Run the existing independently witnessed fidelity campaign and fail closed."""
    from .fidelity import run as fidelity

    recorded_sources = source_revisions(root)
    output = await fidelity(root, reference=True)
    analysis = json.loads((output / "analysis.json").read_text())
    verdict = summarize(analysis)
    if source_revisions(root) != recorded_sources:
        raise ValueError("Reference source snapshots changed during campaign")
    verdict["source_revisions"] = recorded_sources
    write_json(output / "reference-verdict.json", verdict)
