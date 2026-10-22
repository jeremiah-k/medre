"""Recheck native recovery evidence offline without trusting a passing label."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from pathlib import Path

from medre.config.identifiers import adapter_id_problem
from medre.core.storage.sqlite.storage import SQLiteStorage
from scripts.adapter_campaign.recovery import SCENARIOS, read_event
from scripts.adapter_campaign.recovery_evidence import (
    native_deliveries,
    verify_identity,
    verify_native_history,
    verify_preserved,
    verify_terminal,
)


def records(path: Path) -> list[dict]:
    """Read every JSON Lines record in order, propagating file and JSON errors."""
    return [json.loads(line) for line in path.read_text().splitlines()]


async def analyze(directory: Path) -> dict:
    """Return per-scenario verdicts after checking retained recovery evidence.

    Read campaign and case reports, native and runtime control streams, and
    the shared database without changing them. Cross-check identities, source
    acknowledgements, crash timing, delivery bounds, receipt authority, and
    preserved history. Raise AssertionError for contract violations or
    RuntimeError when assertions are disabled; file, JSON, lookup, and storage
    errors propagate.
    """
    if not __debug__:
        raise RuntimeError("Recovery analysis requires enabled assertions")
    report = json.loads((directory / "results.json").read_text())
    assert (
        report["version"] == 1 and report["status"] == "passed"
    ), "Recovery campaign is incomplete"
    assert not report["cleanup_errors"], "Campaign cleanup failed"
    prefix = report["run_prefix"]
    assert isinstance(prefix, str) and re.fullmatch(
        r"MR-recovery-[0-9a-f]{12}-", prefix
    ), "Invalid recovery nonce prefix"
    assert report["expected_scenarios"] == list(
        SCENARIOS
    ), "Unexpected scenario coverage"
    assert [c["scenario"] for c in report["cases"]] == list(
        SCENARIOS
    ), "Missing, extra, or duplicate recovery scenarios"
    topology = report["native_expectations"]
    assert len(topology) == 2 and {v["transport"] for v in topology.values()} == {
        "matrix",
        "xmpp",
    }, "Invalid native topology"
    for identity in topology.values():
        assert all(
            isinstance(identity.get(k), str) and identity[k]
            for k in ("source_channel", "receipt_sender", "receipt_channel")
        ), "Missing native identity expectations"
    source = next(k for k, v in topology.items() if v["transport"] == "matrix")
    target = next(k for k, v in topology.items() if v["transport"] == "xmpp")
    for name in (source, target):
        assert not adapter_id_problem(name), "Invalid endpoint identity"
    incoming = records(directory / (source + ".jsonl"))
    observed = records(directory / (target + ".jsonl"))
    assert not any(
        r["kind"] == "error" for r in incoming + observed
    ), "Native witness failed"
    verify_native_history(report["cases"], observed)
    ready = next(r for r in observed if r["kind"] == "ready")
    assert (
        ready["identity"] != topology[target]["receipt_sender"]
    ), "Native witness is the bot"
    endpoint = {
        "transport": "xmpp",
        "channel": topology[target]["receipt_channel"],
        "expected_sender": topology[target]["receipt_sender"],
        "peer": {},
        "expected_prefixes": report["presentation_expectations"][target],
    }
    assert isinstance(
        endpoint["expected_prefixes"][source], str
    ), "Missing native presentation expectation"
    verdicts = []
    for expected, case in zip(SCENARIOS, report["cases"], strict=True):
        assert (
            case["status"] == "passed" and not case["cleanup_errors"]
        ), "Recovery case failed"
        local = directory / expected
        assert (
            json.loads((local / "results.json").read_text()) == case
        ), "Case and campaign records disagree"
        marker = prefix + expected + " "
        assert case["marker"] == marker, "Recovery case nonce differs"
        acknowledgements = [
            r
            for r in incoming
            if r["kind"] == "sent" and r.get("native_id") == case["source_native_id"]
        ]
        assert (
            len(acknowledgements) == 1 and case["source_native_id"]
        ), "Missing independent source acknowledgement"
        db = await SQLiteStorage.open_readonly(str(directory / "medre.sqlite3"))
        try:
            item = await read_event(db, marker)
        finally:
            await db.close()
        assert (
            item == case["after_recovery"]
        ), "Durable evidence differs from the case report"
        verify_identity(
            item,
            source=source,
            channel=topology[source]["source_channel"],
            native_id=case["source_native_id"],
            target=target,
            target_channel=topology[target]["source_channel"],
        )
        terminal = "dead_lettered" if expected == "retry_exhaustion" else "sent"
        receipt = verify_terminal(item, status=terminal)
        maximum = (
            2
            if expected == "crash_after_handoff"
            else (0 if expected == "retry_exhaustion" else 1)
        )
        hits = native_deliveries(
            observed,
            endpoint,
            marker=marker,
            source=source,
            text=marker + "café ✓",
            minimum=0 if maximum == 0 else 1,
            maximum=maximum,
        )
        assert (
            hits == case["native_receipts"]
        ), "Native witness differs from recorded receipts"
        control = records(local / "runtime-0.jsonl")
        assert not any(
            r["kind"] == "error" for r in control
        ), "Runtime control stream failed"
        if expected.startswith("crash_") or expected == "restart_after_terminal":
            crash = [r for r in control if r["kind"] == "intentional_crash"]
            assert (
                len(crash) == 1 and crash[0]["exit_code"] == -9
            ), "Missing intentional process-death evidence"
            requested = crash[0]["requested_at"]
            assert (
                isinstance(requested, (int, float))
                and not isinstance(requested, bool)
                and math.isfinite(requested)
            ), "Invalid crash timestamp"
            prior = [r for r in hits if r["at"] <= requested]
            assert len(prior) == (
                0 if expected == "crash_before_handoff" else 1
            ), "Native observation did not establish the crash window"
            verify_preserved(case["before_crash"], item)
            resumed = records(local / "runtime-1.jsonl")
            assert any(r["kind"] == "ready" for r in resumed) and not any(
                r["kind"] == "error" for r in resumed
            ), "Resumed runtime did not qualify"
            if expected.startswith("crash_"):
                barriers = [r for r in control if r["kind"] == "barrier"]
                stage = expected.removeprefix("crash_")
                assert (
                    len(barriers) == 1
                    and barriers[0]["stage"] == stage
                    and barriers[0]["event_id"] == item["event"]["event_id"]
                ), "Missing precise handoff barrier"
                assert (
                    not case["before_crash"]["receipts"]
                    and case["before_crash"]["outbox"][0]["status"] == "in_progress"
                ), "Crash window already had committed evidence"
                assert (
                    receipt["attempt_number"] == 2 and receipt["source"] == "retry"
                ), "Interrupted generation did not resume through retry"
                if expected == "crash_after_handoff":
                    assert (
                        case["ambiguity"] == "native_handoff_without_committed_receipt"
                    ), "Ambiguous handoff was concealed"
            else:
                assert (
                    item == case["before_crash"] and receipt["attempt_number"] == 1
                ), "Completed work was redelivered"
        else:
            verify_preserved(case["before_restore"], item)
            assert any(
                r["kind"] == "sent"
                and r.get("gate", {}).get("blocked")
                and r.get("target_health") != "healthy"
                for r in control
            ), "Destination outage was not established"
            assert any(
                r["kind"] == "sent"
                and r.get("gate", {}).get("blocked") is False
                and r.get("target_health") == "healthy"
                for r in control
            ), "Destination recovery was not established"
            if expected == "retry_exhaustion":
                assert [
                    r["attempt_number"]
                    for r in item["receipts"]
                    if r["status"] == "failed"
                ] == [1, 2, 3], "Retry exhaustion exceeded or skipped its budget"
                canary = prefix + expected + "-canary "
                assert (
                    sum(
                        row["kind"] == "sent"
                        and row.get("native_id") == case["canary_source_native_id"]
                        for row in incoming
                    )
                    == 1
                ), "Canary lacks independent source acknowledgement"
                db = await SQLiteStorage.open_readonly(str(directory / "medre.sqlite3"))
                try:
                    canary_evidence = await read_event(db, canary)
                finally:
                    await db.close()
                assert (
                    canary_evidence == case["canary_evidence"]
                ), "Canary durable evidence differs"
                verify_identity(
                    canary_evidence,
                    source=source,
                    channel=topology[source]["source_channel"],
                    native_id=case["canary_source_native_id"],
                    target=target,
                    target_channel=topology[target]["source_channel"],
                )
                verify_terminal(canary_evidence, status="sent")
                witnessed = native_deliveries(
                    observed,
                    endpoint,
                    marker=canary,
                    source=source,
                    text=canary + "café ✓",
                    minimum=1,
                    maximum=1,
                )
                assert (
                    witnessed == case["restored_transport_canary"]
                ), "Healthy transport canary was not witnessed"
            else:
                assert (
                    receipt["attempt_number"] >= 2 and receipt["source"] == "retry"
                ), "Transient work did not retry"
        verdicts.append(
            {
                "scenario": expected,
                "status": "passed",
                "native_deliveries": len(hits),
                "attempt_number": receipt["attempt_number"],
            }
        )
    return {"version": 1, "status": "passed", "scenarios": verdicts}


def main(argv: list[str] | None = None) -> int:
    """Print offline analysis as JSON and return 0 on success.

    Convert analysis exceptions into a generic failure summary and return 1,
    omitting exception text. Argument errors raise SystemExit.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(analyze(args.directory))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "message": "Recovery evidence is invalid or incomplete",
                }
            )
        )
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
