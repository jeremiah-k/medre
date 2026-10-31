"""Qualify native encrypted Matrix traffic with durable restart evidence."""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from uuid import uuid4

from medre.core.storage.sqlite.storage import SQLiteStorage

from .encrypted_evidence import CASES, example, verify_case
from .evidence import wait_evidence
from .lab.sources import write_json
from .process import Peer
from .recovery import observe, read_event


async def run(fixture: dict, directory: Path) -> dict:
    """Require six hard-pass cases and clean owned-process shutdown.

    The directory contains provisioning inputs and receives private native
    records, runtime streams, SQLite state, and a report even on failure.
    Restart reuses device stores and core state without sharing live clients.
    """
    if not __debug__:
        raise RuntimeError("Encrypted qualification requires enabled assertions")
    prefix = "ME-" + uuid4().hex[:12] + "-"
    topology = fixture["expectations"]
    report = dict(
        version=1,
        status="failed",
        run_prefix=prefix,
        expectations=topology,
        cases=[],
        cleanup_errors=[],
    )
    peer = Peer("peer", directory, 90)
    generations = []
    stopped = set()
    ready = []
    storage = None
    failure = None
    try:
        await peer.start(
            {**fixture["peer"], "prefix": prefix, "identities": topology},
            argv=[sys.executable, "-m", "scripts.adapter_campaign.encrypted_peer"],
        )
        for generation in (0, 1):
            bridge = Peer(f"runtime-{generation}", directory, 90)
            generations.append(bridge)
            await bridge.start(
                dict(runtime_file=fixture["runtime_file"], directory=str(directory)),
                argv=[
                    sys.executable,
                    "-m",
                    "scripts.adapter_campaign.encrypted_worker",
                ],
            )
            ready.append(next(r["at"] for r in bridge.records if r["kind"] == "ready"))
            keys = (await peer.command("identities"))["identities"]
            assert keys == {
                name: entry["keys"] for name, entry in topology.items()
            }, "A device crypto identity changed"
            if storage is None:
                storage = await SQLiteStorage.open_readonly(
                    str(directory / "medre.sqlite3")
                )
            for name, source, media, epoch in CASES:
                if epoch != generation:
                    continue
                target = "matrix_target" if source == "matrix" else "matrix"
                expected = example(prefix, name)
                async with asyncio.timeout(120):
                    args = dict(room=topology[source]["room"], text=expected["text"])
                    if media:
                        args.update(
                            bytes_hex=expected["data"].hex(),
                            filename=expected["filename"],
                        )
                    ack = await peer.command("file" if media else "send", **args)
                    for endpoint in (source, target):
                        await peer.wait(
                            lambda r, expected=expected, endpoint=endpoint: r["kind"]
                            == "received"
                            and expected["text"] in r["text"]
                            and r["room"] == topology[endpoint]["room"]
                        )
                    item = await wait_evidence(
                        storage,
                        expected["text"],
                        source=source,
                        channel=topology[source]["room"],
                        targets={target: topology[target]["room"]},
                        timeout=90,
                    )
                    # Compare the same JSON representation retained in the report.
                    item = json.loads(json.dumps(item))
                    case = dict(
                        name=name,
                        source=source,
                        target=target,
                        source_ack=ack["native_id"],
                        core=item,
                    )
                    verify_case(case, item, peer.records, topology, prefix, ready)
                    if media:
                        retained = await storage.load_attachment_content(
                            item["event"]["event_id"], "sha256:" + expected["sha256"]
                        )
                        assert (
                            retained.data == expected["data"]
                        ), "Retained attachment differs from original input"
                    report["cases"].append(case)
            await observe([peer, bridge], 2)
            await bridge.stop()
            stopped.add(bridge)
        keys = (await peer.command("identities"))["identities"]
        assert keys == {name: entry["keys"] for name, entry in topology.items()}
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        (directory / "failure.log").write_text(traceback.format_exc())
    finally:
        for owned in [*reversed(generations), peer]:
            if owned in stopped:
                continue
            try:
                await owned.stop()
            except Exception as exc:
                report["cleanup_errors"].append(
                    dict(process=owned.name, error_type=type(exc).__name__)
                )
        if storage is not None:
            try:
                if failure is None:
                    for case in report["cases"]:
                        item = await read_event(
                            storage, example(prefix, case["name"])["text"]
                        )
                        verify_case(case, item, peer.records, topology, prefix, ready)
            except Exception as exc:
                failure = exc
                report["error_type"] = type(exc).__name__
                (directory / "failure.log").write_text(traceback.format_exc())
            finally:
                try:
                    await storage.close()
                except Exception as exc:
                    report["cleanup_errors"].append(dict(storage=type(exc).__name__))
        if (
            failure is None
            and not report["cleanup_errors"]
            and len(report["cases"]) == len(CASES)
        ):
            report["status"] = "passed"
        write_json(directory / "results.json", report)
    if failure is not None:
        raise failure
    if report["status"] != "passed":
        raise RuntimeError("Encrypted campaign cleanup or evidence failed")
    return report
