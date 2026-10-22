"""Qualify native reply correlation and presentation across Matrix and XMPP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import traceback
from uuid import uuid4

from medre.core.storage.sqlite.storage import SQLiteStorage

from .fidelity_evidence import verify_case
from .plan import Plan, load_plan
from .process import Peer
from .recovery import observe, read_event, wait_event
from .run import write_report

CASES = (
    ("matrix_parent", "matrix", None),
    ("matrix_reply", "matrix", "matrix_parent"),
    ("xmpp_return", "xmpp", "matrix_parent"),
    ("xmpp_parent", "xmpp", None),
    ("xmpp_reply", "xmpp", "xmpp_parent"),
    ("matrix_return", "matrix", "xmpp_parent"),
    ("matrix_after_restart", "matrix", "matrix_parent"),
    ("xmpp_after_restart", "xmpp", "xmpp_parent"),
    ("matrix_missing", "matrix", "missing"),
    ("xmpp_missing", "xmpp", "missing"),
)


def expectations(plan: Plan) -> dict:
    result = {}
    for name, endpoint in plan.endpoints.items():
        peer = endpoint["peer"]
        channel = peer.get("channel", endpoint["channel"])
        result[name] = {
            "transport": endpoint["transport"],
            "channel": endpoint["channel"],
            "peer_identity": (
                peer["user_id"]
                if endpoint["transport"] == "matrix"
                else channel + "/" + peer["nickname"]
            ),
            "bot_identity": endpoint["expected_sender"],
            "prefixes": endpoint["expected_prefixes"],
        }
        if channel != endpoint["channel"]:
            raise ValueError("Fidelity peers must observe the routed conversation")
    return result


async def run(plan: Plan, directory: Path) -> dict:
    if not __debug__:
        raise RuntimeError("Fidelity qualification requires enabled assertions")
    if len(plan.endpoints) != 2 or {
        e["transport"] for e in plan.endpoints.values()
    } != {"matrix", "xmpp"}:
        raise ValueError("Fidelity requires one Matrix and one XMPP endpoint")
    topology = expectations(plan)
    by_transport = {v["transport"]: k for k, v in topology.items()}
    directory.mkdir(mode=0o700)
    prefix = "MF-" + uuid4().hex[:12] + "-"
    peers = {name: Peer(name, directory, plan.timeout) for name in plan.endpoints}
    generations = []
    stopped = set()
    storage = None
    report = {
        "version": 1,
        "status": "running",
        "run_prefix": prefix,
        "expectations": topology,
        "cases": [],
    }
    failure = None

    async def start():
        bridge = Peer(f"runtime-{len(generations)}", directory, plan.timeout)
        generations.append(bridge)
        await bridge.start(
            {
                "runtime_file": str(plan.runtime_file),
                "directory": str(directory),
                "endpoints": plan.endpoints,
            },
            argv=[sys.executable, "-m", "scripts.adapter_campaign.fidelity_worker"],
        )
        return bridge

    try:
        for name, peer in peers.items():
            await peer.start({**plan.endpoints[name], "marker_prefix": prefix})
            ready = next(r for r in peer.records if r["kind"] == "ready")
            assert (
                ready["identity"]
                == topology[name]["peer_identity"]
                != topology[name]["bot_identity"]
            ), "Native witness is not the independent configured peer"
        bridge = await start()
        storage = await SQLiteStorage.open_readonly(str(directory / "medre.sqlite3"))
        parents = {}
        for name, transport, parent_name in CASES:
            if name == "matrix_after_restart":
                # Shutdown and readiness have their own bounded budget; neither
                # may consume the native reply observation deadline.
                async with asyncio.timeout(plan.timeout + 30):
                    await bridge.stop()
                    stopped.add(bridge)
                    bridge = await start()
            async with asyncio.timeout(plan.timeout):
                source = by_transport[transport]
                target = next(k for k in peers if k != source)
                parent = parents.get(parent_name)
                marker = prefix + name + " "
                case = {
                    "name": name,
                    "source": source,
                    "target": target,
                    "marker": marker,
                    "text": marker + "café 🦉",
                    "parent": parent_name,
                    "mode": "missing" if parent_name == "missing" else "native",
                }
                if parent is not None:
                    case["native_target"] = (
                        parent["source_id"]
                        if source == parent["source"]
                        else parent["target_id"]
                    )
                elif parent_name == "missing":
                    case["native_target"] = (
                        "$missing-" + prefix
                        if transport == "matrix"
                        else "missing-" + prefix
                    )
                else:
                    case["native_target"] = None
                ack = await (
                    peers[source].reply(case["text"], case["native_target"])
                    if parent_name
                    else peers[source].send(case["text"])
                )
                case["source_ack"] = ack["native_id"]
                item = await wait_event(
                    storage,
                    marker,
                    lambda r: len(r["outbox"]) == 1
                    and r["outbox"][0]["status"] == "sent",
                    [*peers.values(), bridge],
                )
                await peers[source].wait(
                    lambda r, nonce=marker: r["kind"] == "received"
                    and nonce in r.get("text", "")
                )
                await peers[target].wait(
                    lambda r, nonce=marker: r["kind"] == "received"
                    and nonce in r.get("text", "")
                )
                await observe([*peers.values(), bridge], plan.settle)
                case.update(
                    verify_case(
                        case,
                        item,
                        peers[source].records,
                        peers[target].records,
                        topology,
                        parents,
                    )
                )
                case["core"] = item
                parents[name] = case
                report["cases"].append(case)
                write_report(directory, report)
                print("PASS fidelity: " + name, flush=True)
        # Re-evaluate early cases after all later replies and the restart.
        for case in report["cases"]:
            current = await read_event(storage, case["marker"])
            assert current == case["core"], "Later work rewrote parent evidence"
            verify_case(
                case,
                current,
                peers[case["source"]].records,
                peers[case["target"]].records,
                topology,
                parents,
            )
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        (directory / "failure.log").write_text(traceback.format_exc())
    finally:
        cleanup = []
        for bridge in reversed(generations):
            if bridge not in stopped:
                try:
                    await bridge.stop()
                except Exception as exc:
                    cleanup.append(
                        {"runtime": bridge.name, "error_type": type(exc).__name__}
                    )
        for name, peer in peers.items():
            try:
                await peer.stop()
            except Exception as exc:
                cleanup.append({"peer": name, "error_type": type(exc).__name__})
        if storage is not None:
            try:
                await storage.close()
            except Exception as exc:
                cleanup.append(
                    {"storage": "readonly_evidence", "error_type": type(exc).__name__}
                )
        if failure is None:
            try:
                for case in report["cases"]:
                    verify_case(
                        case,
                        case["core"],
                        peers[case["source"]].records,
                        peers[case["target"]].records,
                        topology,
                        parents,
                    )
            except Exception as exc:
                failure = exc
                report["error_type"] = type(exc).__name__
                (directory / "failure.log").write_text(traceback.format_exc())
        report["cleanup_errors"] = cleanup
        report["status"] = (
            "passed"
            if failure is None and not cleanup and len(report["cases"]) == len(CASES)
            else "failed"
        )
        write_report(directory, report)
    if failure is not None:
        raise failure
    if report["status"] != "passed":
        raise RuntimeError("Fidelity cleanup failed")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    plan = load_plan(args.config, only={"matrix", "xmpp"})
    if not args.run:
        print(
            json.dumps(
                {"cases": [case[0] for case in CASES], "transports": ["matrix", "xmpp"]}
            )
        )
        return 0
    if args.output is None:
        parser.error("--run requires a fresh private --output directory")
    asyncio.run(run(plan, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
