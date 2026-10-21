"""Bounded native process-crash and destination-outage qualification."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from uuid import uuid4

from medre.core.storage.sqlite.storage import SQLiteStorage

from .evidence import snapshots
from .plan import Plan, load_plan
from .process import Peer
from .recovery_evidence import (
    native_deliveries,
    verify_native_history,
    verify_identity,
    verify_preserved,
    verify_terminal,
)
from .run import native_expectation, write_report

SCENARIOS = (
    "crash_before_handoff",
    "crash_after_handoff",
    "restart_after_terminal",
    "transient_outage",
    "retry_exhaustion",
)


def runtime_generations(scenario: str) -> tuple[str, ...]:
    if scenario not in SCENARIOS:
        raise ValueError("Unknown recovery scenario")
    count = (
        2
        if scenario.startswith("crash_") or scenario == "restart_after_terminal"
        else 1
    )
    return tuple(f"runtime-{number}" for number in range(count))


async def read_event(storage: SQLiteStorage, marker: str) -> dict | None:
    items = await snapshots(storage, marker)
    assert len(items) <= 1, "Recovery duplicated canonical ingress"
    # Match the report's JSON representation, including nested tuple fields.
    return json.loads(json.dumps(items[0], default=str)) if items else None


async def wait_event(storage, marker, predicate, peers) -> dict:
    while True:
        for peer in peers:
            peer.check()
        item = await read_event(storage, marker)
        if item is not None and predicate(item):
            return item
        await asyncio.sleep(0.1)


async def wait_health(bridge: Peer, *, healthy: bool) -> None:
    while True:
        status = await bridge.command("status")
        if (status["target_health"] == "healthy") is healthy:
            return
        await asyncio.sleep(0.1)


async def observe(peers: list[Peer], seconds: float) -> None:
    until = asyncio.get_running_loop().time() + seconds
    while (remaining := until - asyncio.get_running_loop().time()) > 0:
        for peer in peers:
            peer.check()
        await asyncio.sleep(min(0.1, remaining))


async def case(
    plan: Plan, directory: Path, name: str, peers: dict[str, Peer], prefix: str
) -> dict:
    directory.mkdir(mode=0o700)
    source = next(k for k, v in plan.endpoints.items() if v["transport"] == "matrix")
    target = next(k for k, v in plan.endpoints.items() if v["transport"] == "xmpp")
    marker = prefix + name + " "
    text = marker + "café ✓"
    settings = {
        "runtime_file": str(plan.runtime_file),
        "directory": str(directory),
        "database": str(directory.parent / "medre.sqlite3"),
        "source": source,
        "target": target,
        "source_channel": plan.endpoints[source]["channel"],
        "target_channel": plan.endpoints[target]["channel"],
        "marker": marker,
        "barrier": {
            "crash_before_handoff": "before_handoff",
            "crash_after_handoff": "after_handoff",
        }.get(name),
    }
    generations: list[Peer] = []
    stopped: set[Peer] = set()
    storage = None
    report = {
        "scenario": name,
        "status": "running",
        "marker": marker,
        "started_at": time.time(),
    }

    async def start(barrier):
        bridge = Peer(
            runtime_generations(name)[len(generations)], directory, plan.timeout
        )
        generations.append(bridge)
        await bridge.start(
            {**settings, "barrier": barrier},
            argv=[sys.executable, "-m", "scripts.adapter_campaign.recovery_worker"],
        )
        return bridge

    def check_native(minimum, maximum, *, nonce=marker, message=text):
        return native_deliveries(
            peers[target].records,
            plan.endpoints[target],
            marker=nonce,
            source=source,
            text=message,
            minimum=minimum,
            maximum=maximum,
        )

    def identity(item):
        return verify_identity(
            item,
            source=source,
            channel=settings["source_channel"],
            native_id=report["source_native_id"],
            target=target,
            target_channel=settings["target_channel"],
        )

    try:
        bridge = await start(settings["barrier"])
        storage = await SQLiteStorage.open_readonly(settings["database"])
        if name in ("transient_outage", "retry_exhaustion"):
            await bridge.command("block")
            await wait_health(bridge, healthy=False)
        report["source_native_id"] = (await peers[source].send(text))["native_id"]
        assert report["source_native_id"], "Native source did not return an event ID"
        if name.startswith("crash_"):
            barrier = await bridge.wait(
                lambda r: r["kind"] == "barrier"
                and r.get("stage") == settings["barrier"]
            )
            before = await wait_event(
                storage, marker, lambda r: len(r["outbox"]) == 1, list(peers.values())
            )
            assert before["event"]["event_id"] == barrier["event_id"]
            row = identity(before)
            assert (
                row["status"] == "in_progress" and not before["receipts"]
            ), "Crash barrier missed the uncommitted handoff window"
            if name == "crash_after_handoff":
                await peers[target].wait(
                    lambda r: r["kind"] == "received" and marker in r["text"]
                )
                check_native(1, 1)
            else:
                check_native(0, 0)
            report["before_crash"] = before
            report["crash_exit"] = await bridge.crash()
            await bridge.stop()
            stopped.add(bridge)
            bridge = await start(None)
            after = await wait_event(
                storage,
                marker,
                lambda r: len(r["outbox"]) == 1 and r["outbox"][0]["status"] == "sent",
                list(peers.values()) + [bridge],
            )
            receipt = verify_terminal(after, status="sent")
            assert (
                receipt["attempt_number"] == 2 and receipt["source"] == "retry"
            ), "Restart did not preserve the interrupted attempt generation"
            verify_preserved(before, after)
            identity(after)
            await peers[target].wait(
                lambda r: r["kind"] == "received" and marker in r["text"]
            )
            await observe(list(peers.values()) + [bridge], plan.settle)
            report["native_receipts"] = check_native(
                1, 2 if name == "crash_after_handoff" else 1
            )
            report["ambiguity"] = (
                "native_handoff_without_committed_receipt"
                if name == "crash_after_handoff"
                else None
            )
        elif name == "restart_after_terminal":
            before = await wait_event(
                storage,
                marker,
                lambda r: len(r["outbox"]) == 1 and r["outbox"][0]["status"] == "sent",
                list(peers.values()) + [bridge],
            )
            identity(before)
            verify_terminal(before, status="sent")
            await peers[target].wait(
                lambda r: r["kind"] == "received" and marker in r["text"]
            )
            check_native(1, 1)
            report["before_crash"] = before
            report["crash_exit"] = await bridge.crash()
            await bridge.stop()
            stopped.add(bridge)
            bridge = await start(None)
            await observe(list(peers.values()) + [bridge], plan.settle)
            after = await read_event(storage, marker)
            assert after == before, "Completed work changed across restart"
            report["native_receipts"] = check_native(1, 1)
        else:
            before = await wait_event(
                storage,
                marker,
                lambda r: any(x["status"] == "failed" for x in r["receipts"]),
                list(peers.values()) + [bridge],
            )
            identity(before)
            check_native(0, 0)
            report["before_restore"] = before
            if name == "retry_exhaustion":
                after = await wait_event(
                    storage,
                    marker,
                    lambda r: len(r["outbox"]) == 1
                    and r["outbox"][0]["status"] == "dead_lettered",
                    list(peers.values()) + [bridge],
                )
                receipt = verify_terminal(after, status="dead_lettered")
                assert receipt["attempt_number"] == 3
                assert [
                    r["attempt_number"]
                    for r in after["receipts"]
                    if r["status"] == "failed"
                ] == [
                    1,
                    2,
                    3,
                ], "Retry exhaustion did not use the configured attempt budget"
            await bridge.command("restore")
            await wait_health(bridge, healthy=True)
            if name == "transient_outage":
                after = await wait_event(
                    storage,
                    marker,
                    lambda r: len(r["outbox"]) == 1
                    and r["outbox"][0]["status"] == "sent",
                    list(peers.values()) + [bridge],
                )
                receipt = verify_terminal(after, status="sent")
                assert receipt["attempt_number"] >= 2 and receipt["source"] == "retry"
                await peers[target].wait(
                    lambda r: r["kind"] == "received" and marker in r["text"]
                )
            else:
                canary = prefix + name + "-canary "
                message = canary + "café ✓"
                sent = await peers[source].send(message)
                report["canary_source_native_id"] = sent["native_id"]
                assert sent["native_id"], "Native canary lacks source correlation"
                canary_evidence = await wait_event(
                    storage,
                    canary,
                    lambda r: len(r["outbox"]) == 1
                    and r["outbox"][0]["status"] == "sent",
                    list(peers.values()) + [bridge],
                )
                verify_identity(
                    canary_evidence,
                    source=source,
                    channel=settings["source_channel"],
                    native_id=sent["native_id"],
                    target=target,
                    target_channel=settings["target_channel"],
                )
                verify_terminal(canary_evidence, status="sent")
                report["canary_evidence"] = canary_evidence
                await peers[target].wait(
                    lambda r: r["kind"] == "received" and canary in r["text"]
                )
                report["restored_transport_canary"] = check_native(
                    1, 1, nonce=canary, message=message
                )
            await observe(list(peers.values()) + [bridge], plan.settle)
            if name == "retry_exhaustion":
                assert after == await read_event(
                    storage, marker
                ), "Terminal failed work revived after transport recovery"
            verify_preserved(before, after)
            identity(after)
            report["native_receipts"] = check_native(
                0 if name == "retry_exhaustion" else 1,
                0 if name == "retry_exhaustion" else 1,
            )
        report["after_recovery"] = after
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        (directory / "failure.log").write_text(traceback.format_exc())
        raise
    finally:
        cleanup = []
        for bridge in reversed(generations):
            if bridge in stopped:
                continue
            try:
                await bridge.stop()
            except Exception as exc:
                cleanup.append(
                    {"process": bridge.name, "error_type": type(exc).__name__}
                )
        if storage is not None:
            try:
                await storage.close()
            except Exception as exc:
                cleanup.append(
                    {"storage": "readonly_evidence", "error_type": type(exc).__name__}
                )
        report["cleanup_errors"] = cleanup
        report["finished_at"] = time.time()
        if cleanup:
            report["status"] = "failed"
        write_report(directory, report)
        if cleanup:
            raise RuntimeError("Recovery runtime cleanup failed")
    return report


async def run(plan: Plan, directory: Path, *, timeout: float = 150) -> dict:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 1 <= timeout <= 600
    ):
        raise ValueError(
            "Scenario timeout must be finite and between 1 and 600 seconds"
        )
    if not __debug__ or os.name != "posix":
        raise RuntimeError(
            "Native crash qualification requires POSIX and enabled assertions"
        )
    if len(plan.endpoints) != 2 or {
        e["transport"] for e in plan.endpoints.values()
    } != {"matrix", "xmpp"}:
        raise ValueError("Recovery requires exactly one Matrix and one XMPP endpoint")
    directory.mkdir(mode=0o700)
    prefix = "MR-recovery-" + uuid4().hex[:12] + "-"
    report = {
        "version": 1,
        "run_prefix": prefix,
        "status": "running",
        "expected_scenarios": list(SCENARIOS),
        "cases": [],
        "native_expectations": {
            k: native_expectation(v) for k, v in plan.endpoints.items()
        },
        "presentation_expectations": {
            k: v["expected_prefixes"] for k, v in plan.endpoints.items()
        },
    }
    write_report(directory, report)
    peers = {k: Peer(k, directory, plan.timeout) for k in plan.endpoints}
    failure = None
    active = None
    try:
        for name, peer in peers.items():
            await peer.start({**plan.endpoints[name], "marker_prefix": prefix})
            ready = next(r for r in peer.records if r["kind"] == "ready")
            assert (
                ready["identity"] != plan.endpoints[name]["expected_sender"]
            ), "Native peer must be independent of the bot"
        for name in SCENARIOS:
            active = name
            async with asyncio.timeout(timeout):
                result = await case(plan, directory / name, name, peers, prefix)
            report["cases"].append(result)
            write_report(directory, report)
            print("PASS recovery: " + name, flush=True)
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        (directory / "failure.log").write_text(traceback.format_exc())
        if active is not None:
            evidence = directory / active / "results.json"
            report["cases"].append(
                json.loads(evidence.read_text())
                if evidence.exists()
                else {
                    "scenario": active,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
    finally:
        cleanup = []
        for name, peer in peers.items():
            try:
                await peer.stop()
            except Exception as exc:
                cleanup.append({"peer": name, "error_type": type(exc).__name__})
        if failure is None:
            try:
                target = next(
                    k for k, v in plan.endpoints.items() if v["transport"] == "xmpp"
                )
                verify_native_history(report["cases"], peers[target].records)
            except Exception as exc:
                failure = exc
                report["error_type"] = type(exc).__name__
                (directory / "failure.log").write_text(traceback.format_exc())
        report["cleanup_errors"] = cleanup
        report["status"] = (
            "passed"
            if failure is None
            and not cleanup
            and len(report["cases"]) == len(SCENARIOS)
            else "failed"
        )
        write_report(directory, report)
    if failure is not None:
        raise failure
    if report["status"] != "passed":
        raise RuntimeError("Recovery campaign did not finish cleanly")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    plan = load_plan(args.config, only={"matrix", "xmpp"})
    if not args.run:
        print(
            json.dumps(
                {
                    "scenarios": SCENARIOS,
                    "transports": ["matrix", "xmpp"],
                    "scope": "disposable-runtime-only",
                },
                indent=2,
            )
        )
        return 0
    if args.output is None:
        parser.error("--run requires a fresh private --output directory")
    asyncio.run(run(plan, args.output, timeout=args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
