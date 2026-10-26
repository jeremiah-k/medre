"""Qualify transient typing with independent native peers and durable row counts."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from .fidelity import expectations
from .plan import Plan
from .process import Peer
from .recovery import observe
from .run import write_report

TABLES = ("canonical_events", "delivery_outbox", "delivery_receipts")


def counts(directory: Path) -> dict[str, int]:
    """Inspect actual durable tables independently of runtime activity counters."""
    uri = (directory / "medre.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        return {
            table: database.execute(
                f"SELECT COUNT(*) FROM {table}"  # nosec B608 - table names come from the module-level TABLES constant
            ).fetchone()[0]
            for table in TABLES
        }


def matches(record: dict, expected: dict, active: bool, since: float) -> bool:
    """Require the configured native conversation and relay identity as witness."""
    if (
        record.get("kind") != "typing"
        or record.get("at", 0) <= since
        or record.get("channel") != expected["channel"]
    ):
        return False
    if expected["transport"] == "matrix":
        users = record.get("users")
        return (
            isinstance(users, list)
            and all(isinstance(user, str) for user in users)
            and ((expected["bot_identity"] in users) is active)
        )
    return record.get("sender") == expected["bot_identity"] and record.get("state") in (
        {"composing"} if active else {"active", "paused", "inactive"}
    )


async def run(plan: Plan, directory: Path) -> dict:
    """Witness both directions, expiry, withdrawal, shutdown and default refusal.

    Native absence checks cover a bounded observation window, never an
    unbounded claim. Every child and its private evidence streams are owned.
    """
    if not __debug__:
        raise RuntimeError("Typing qualification requires enabled assertions")
    if len(plan.endpoints) != 2 or {
        value["transport"] for value in plan.endpoints.values()
    } != {"matrix", "xmpp"}:
        raise ValueError("Typing requires exactly one Matrix and one XMPP endpoint")
    topology = expectations(plan)
    directory.mkdir(mode=0o700)
    peers = {name: Peer(name, directory, plan.timeout) for name in topology}
    children = list(peers.values())
    stopped = set()
    report = dict(version=1, status="running", expectations=topology, cases=[])
    failure = None

    async def start(enabled: bool) -> Peer:
        """Create a separate runtime generation against the same durable store."""
        bridge = Peer("runtime-" + str(len(children) - len(peers)), directory, 60)
        children.append(bridge)
        await bridge.start(
            dict(
                runtime_file=str(plan.runtime_file),
                directory=str(directory),
                endpoints=plan.endpoints,
                forward_typing=enabled,
            ),
            argv=[sys.executable, "-m", "scripts.adapter_campaign.typing_worker"],
        )
        return bridge

    async def witness(target: str, active: bool, since: float) -> dict:
        """Wait for evidence captured after this independently authored case."""
        return await peers[target].wait(
            lambda row: matches(row, topology[target], active, since)
        )

    try:
        for name, peer in peers.items():
            await peer.start(
                {
                    **plan.endpoints[name],
                    "marker_prefix": "MT-NO-MESSAGES-",
                    "observe_typing": True,
                }
            )
            ready = next(row for row in peer.records if row["kind"] == "ready")
            assert ready["identity"] == topology[name]["peer_identity"]
            assert ready["identity"] != topology[name]["bot_identity"]
        bridge = await start(True)
        baseline = await asyncio.to_thread(counts, directory)
        report["baseline"] = baseline
        for source in peers:
            target = next(name for name in peers if name != source)
            since = time.time()
            await peers[source].command("typing", typing=True)
            positive = await witness(target, True, since)
            since = time.time()
            await peers[source].command("typing", typing=False)
            negative = await witness(target, False, since)
            report["cases"].append(
                dict(name=source + "_start_stop", positive=positive, negative=negative)
            )
        # Observe the bounded destination lifetime after a silent native source.
        source = next(name for name in peers if topology[name]["transport"] == "xmpp")
        target = next(name for name in peers if name != source)
        since = time.time()
        await peers[source].command("typing", typing=True)
        positive = await witness(target, True, since)
        negative = await witness(target, False, positive["at"])
        assert negative["at"] - since <= 40, "Silent source exceeded bounded expiry"
        report["cases"].append(
            dict(name="silent_expiry", positive=positive, negative=negative)
        )
        await peers[source].command("typing", typing=False)
        since = time.time()
        await peers[source].command("typing", typing=True)
        await witness(target, True, since)
        since = time.time()
        await bridge.command("withdraw")
        negative = await witness(target, False, since)
        report["cases"].append(dict(name="permission_withdrawal", negative=negative))
        await peers[source].command("typing", typing=False)
        await bridge.stop()
        stopped.add(bridge)
        bridge = await start(True)
        since = time.time()
        await peers[source].command("typing", typing=True)
        await witness(target, True, since)
        since = time.time()
        await bridge.stop()
        stopped.add(bridge)
        negative = await witness(target, False, since)
        report["cases"].append(dict(name="shutdown_clear", negative=negative))
        await peers[source].command("typing", typing=False)
        bridge = await start(False)
        since = time.time()
        for peer in peers.values():
            await peer.command("typing", typing=True)
        await observe([*peers.values(), bridge], max(3, plan.settle))
        assert not any(
            matches(row, topology[name], True, since)
            for name, peer in peers.items()
            for row in peer.records
        ), "Default-off routes disclosed activity"
        report["cases"].append(
            dict(name="default_off", observation_seconds=max(3, plan.settle))
        )
    except BaseException as exc:
        failure = exc
    finally:
        for child in reversed(children):
            if child in stopped:
                continue
            try:
                await child.stop()
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is None:
            try:
                report["final"] = await asyncio.to_thread(counts, directory)
                assert (
                    report["final"] == baseline
                ), "Typing created durable message work"
            except BaseException as exc:
                failure = exc
        report["status"] = "failed" if failure else "passed"
        if failure:
            report["error_type"] = type(failure).__name__
        write_report(directory, report)
    if failure:
        raise failure
    return report
