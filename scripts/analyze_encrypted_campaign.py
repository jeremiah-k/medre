"""Recheck encrypted native witnesses and retained bytes without any server."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from medre.core.storage.sqlite.storage import SQLiteStorage
from scripts.adapter_campaign.encrypted_evidence import CASES, example, verify_case
from scripts.adapter_campaign.lab.log_review import analyze as analyze_log
from scripts.adapter_campaign.recovery import read_event


def records(path: Path) -> list[dict]:
    """Require a retained, bounded native control stream with no error record."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert 0 < len(rows) <= 1024 and not any(r["kind"] == "error" for r in rows)
    return rows


async def analyze(directory: Path) -> dict:
    """Reject incomplete evidence and independently verify all six scenarios.

    No credential files or SDKs are read. SQLite is opened strictly read-only;
    expected bytes and routes come from the nonce and provisioning identities,
    never from an outbox or receiving event under examination.
    """
    if not __debug__:
        raise RuntimeError("Encrypted analysis requires enabled assertions")
    report = json.loads((directory / "results.json").read_text())
    assert type(report["version"]) is int and report["version"] == 1
    assert report["status"] == "passed" and not report["cleanup_errors"]
    topology = report["expectations"]
    assert set(topology) == {"matrix", "matrix_target", "peer"}
    assert len({r["user_id"] for r in topology.values()}) == 3
    assert topology["matrix"]["room"] != topology["matrix_target"]["room"]
    for row in topology.values():
        assert row["user_id"] and row["device_id"]
        assert set(row["keys"]) == {"curve25519", "ed25519"} and all(
            row["keys"].values()
        )
    peer = records(directory / "peer.jsonl")
    peer_ready = [r for r in peer if r["kind"] == "ready"]
    assert len(peer_ready) == 1
    for key in ("identity", "device_id", "keys"):
        assert (
            peer_ready[0][key]
            == topology["peer"]["user_id" if key == "identity" else key]
        )
    identities = [r for r in peer if "identities" in r]
    assert len(identities) == 3
    assert all(
        r["identities"] == {name: entry["keys"] for name, entry in topology.items()}
        for r in identities
    )
    ready = []
    for name in ("runtime-0", "runtime-1", "peer"):
        rows = records(directory / (name + ".jsonl"))
        starts = [r for r in rows if r["kind"] == "ready"]
        assert len(starts) == 1
        if name != "peer":
            assert starts[0]["identity"] == "encrypted-runtime"
            ready.append(starts[0]["at"])
        for suffix in ("stdout", "stderr"):
            log = analyze_log((directory / (name + "." + suffix)).read_text())
            assert (
                not log["resource_warning_lines"]
                and not log["records_without_timestamp"]
            )
            if name != "peer" and suffix == "stdout":
                assert log["levels"], "Missing runtime log records"
    assert peer_ready[0]["at"] < ready[0] < ready[1]
    assert identities[0]["at"] > ready[0] and identities[1]["at"] > ready[1]
    assert identities[2]["at"] > identities[1]["at"]
    assert [c["name"] for c in report["cases"]] == [c[0] for c in CASES]
    db = await SQLiteStorage.open_readonly(str(directory / "medre.sqlite3"))
    try:
        for case in report["cases"]:
            expected = example(report["run_prefix"], case["name"])
            current = await read_event(db, expected["text"])
            verify_case(case, current, peer, topology, report["run_prefix"], ready)
            if "attachment" in current["event"]["payload"]:
                retained = await db.load_attachment_content(
                    current["event"]["event_id"], "sha256:" + expected["sha256"]
                )
                assert (
                    retained.data == expected["data"]
                ), "Retained bytes differ from original fixture"
    finally:
        await db.close()
    return dict(
        status="passed",
        cases=len(CASES),
        encrypted_events=12,
        encrypted_files=6,
        runtime_generations=2,
        trust_policy="unverified_devices_allowed",
        cross_signing="separate_oauth_stage",
    )


def main(argv: list[str] | None = None) -> int:
    """Print a compact verdict; leave private evidence out of failure messages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(analyze(args.directory))
    except Exception as exc:
        print(json.dumps(dict(status="failed", error_type=type(exc).__name__)))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
