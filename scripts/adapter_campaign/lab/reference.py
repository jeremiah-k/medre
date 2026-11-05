"""Opt-in Matrix/XMPP reference journey using the native fidelity oracle.

A positive verdict requires native peers at both ends, an independently
observed runtime restart, and read-only reconciliation with the SQLite ledger.
No sample/fake observation or unexecuted Docker stage can write a pass verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .sources import write_json

DIRECTIONS = ("matrix", "xmpp")
IMAGE_SOURCE_ROOT = Path("/sources")


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


def verify_source_material(root: Path) -> None:
    """Match recorded snapshot bytes to the build context and running image.

    The build snapshot holds exactly the tracked files that were hashed at
    prepare time. The image may add packaging artifacts during installation,
    so compare those same tracked paths inside the image rather than hashing
    its entire post-install tree.
    """
    inventory = json.loads((root / "sources.json").read_text())
    if not isinstance(inventory, dict):
        raise ValueError("Reference source inventory is invalid")
    for name in ("medre", "matrix", "xmpp"):
        record = inventory.get(name)
        if (
            not isinstance(record, dict)
            or type(record.get("files")) is not int
            or record["files"] < 1
            or not isinstance(record.get("snapshot_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["snapshot_sha256"])
        ):
            raise ValueError("Reference source snapshot is incomplete")
        staged = root / "build" / "sources" / name
        image = IMAGE_SOURCE_ROOT / name
        if not staged.is_dir() or not image.is_dir():
            raise ValueError("Reference source material is missing")
        files = sorted(
            staged.rglob("*"),
            key=lambda path: path.relative_to(staged).as_posix().encode(),
        )
        digest = hashlib.sha256()
        total = 0
        for path in files:
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError("Reference source snapshot has an unsafe entry")
            if path.is_dir():
                continue
            relative = path.relative_to(staged)
            encoded = relative.as_posix().encode()
            data = path.read_bytes()
            executable = (path.stat().st_mode & 0o111).to_bytes(2, "big")
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            digest.update(len(data).to_bytes(8, "big") + data)
            digest.update(executable)
            image_path = image / relative
            if (
                image_path.is_symlink()
                or not image_path.is_file()
                or image_path.read_bytes() != data
            ):
                raise ValueError("Reference image differs from recorded source")
            total += 1
        if total != record["files"] or digest.hexdigest() != record["snapshot_sha256"]:
            raise ValueError("Reference build snapshot differs from recorded source")


async def run(root: Path) -> None:
    """Run the existing independently witnessed fidelity campaign and fail closed."""
    from .fidelity import run as fidelity

    recorded_sources = source_revisions(root)
    verify_source_material(root)
    output = await fidelity(root, reference=True)
    analysis = json.loads((output / "analysis.json").read_text())
    verdict = summarize(analysis)
    if source_revisions(root) != recorded_sources:
        raise ValueError("Reference source snapshots changed during campaign")
    verify_source_material(root)
    verdict["source_revisions"] = recorded_sources
    write_json(output / "reference-verdict.json", verdict)
