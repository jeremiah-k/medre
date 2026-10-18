"""One bounded native ingress per adapter, fanout to every other adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import traceback
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from medre.runtime.builder import RuntimeBuilder

from .evidence import snapshots, verify, wait_evidence
from .plan import Plan, runtime_config
from .process import Peer


def write_report(directory: Path, report: dict) -> None:
    temporary = directory / "results.json.tmp"
    temporary.write_text(json.dumps(report, indent=2, default=str) + "\n")
    temporary.replace(directory / "results.json")


def native_receipt(peer: Peer, endpoint: dict, marker: str) -> dict:
    channel = endpoint["peer"].get("channel", endpoint["channel"])
    # LXMF addresses the remote destination, not a shared room/channel.
    if endpoint["transport"] == "lxmf":
        channel = endpoint["expected_sender"]
    hits = [
        record
        for record in peer.records
        if record["kind"] == "received"
        and marker in record["text"]
        and record["sender"] == endpoint["expected_sender"]
    ]
    assert len(hits) == 1, f"{peer.name}: missing or duplicate native delivery"
    assert hits[0]["channel"] == str(
        channel
    ), f"{peer.name}: incorrect native conversation"
    assert "café ✓" in hits[0]["text"], f"{peer.name}: UTF-8 body was altered"
    assert "\ufffd" not in hits[0]["text"], f"{peer.name}: invalid UTF-8 rendering"
    assert hits[0]["text"].count(marker) == 1, f"{peer.name}: duplicated rendered body"
    return hits[0]


async def run(plan: Plan, directory: Path) -> dict:
    if not __debug__:
        raise RuntimeError("Qualification requires enabled assertions")
    directory.mkdir(mode=0o700)  # Refuse reuse, symlinks, and concurrent runs.
    run_id = uuid4().hex[:12]
    prefix = f"MR-{run_id}-"
    report = {
        "run": run_id,
        "status": "running",
        "coverage": "full" if plan.full else "partial",
        "expected_pairs": plan.pairs,
        "completed_pairs": [],
        "cases": [],
        "python": platform.python_version(),
        "packages": {
            "medre": version("medre"),
            **{
                f"medre-adapter-{entry['transport']}": version(
                    f"medre-adapter-{entry['transport']}"
                )
                for entry in plan.endpoints.values()
            },
        },
        "harness_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(__file__).parent.glob("*.py")
        },
        "runtime_config_sha256": hashlib.sha256(
            plan.runtime_file.read_bytes()
        ).hexdigest(),
    }
    write_report(directory, report)
    peers = {name: Peer(name, directory, plan.timeout) for name in plan.endpoints}
    app = None
    failure = None
    try:
        config, paths = runtime_config(plan, directory)
        for name, peer in peers.items():
            await peer.start({**plan.endpoints[name], "marker_prefix": prefix})
            ready = next(record for record in peer.records if record["kind"] == "ready")
            if ready["identity"] == plan.endpoints[name]["expected_sender"]:
                raise ValueError(f"{name}: native peer must be independent of the bot")
        app = RuntimeBuilder(config, paths).build()
        await asyncio.wait_for(app.start(), plan.timeout)
        if app.build_failures or set(app.started_adapter_ids) != set(peers):
            raise RuntimeError("Every selected real adapter must start")
        (directory / "readiness.json").write_text(
            json.dumps(
                {
                    "peers": {
                        name: next(
                            record
                            for record in peer.records
                            if record["kind"] == "ready"
                        )
                        for name, peer in peers.items()
                    },
                    "adapters": {
                        name: adapter.diagnostics()
                        for name, adapter in app.adapters.items()
                    },
                },
                indent=2,
                default=str,
            )
        )
        async with asyncio.timeout(plan.timeout):
            while True:
                health = [
                    await adapter.health_check() for adapter in app.adapters.values()
                ]
                if all(info.health == "healthy" for info in health):
                    break
                await asyncio.sleep(0.1)
        # Peers start before the bridge, so LXMF's announce/path discovery
        # must finish after the bridge identity becomes available.
        for peer in peers.values():
            await peer.synchronize()
        for source, peer in peers.items():
            for owner in peers.values():
                owner.check()
            marker = prefix + source
            text = marker + " café ✓"
            sent = await peer.send(text)
            targets = {
                name: endpoint["channel"]
                for name, endpoint in plan.endpoints.items()
                if name != source
            }
            # Both checks run concurrently: native receipt and durable handoff
            # are independent facts, regardless of which arrives first.
            async with asyncio.TaskGroup() as tasks:
                durable = tasks.create_task(
                    wait_evidence(
                        app.storage,
                        marker,
                        source=source,
                        channel=plan.endpoints[source]["channel"],
                        targets=targets,
                        timeout=plan.timeout,
                    )
                )
                for target in targets:
                    tasks.create_task(
                        peers[target].wait(
                            lambda record, target=target, marker=marker: record["kind"]
                            == "received"
                            and marker in record["text"]
                            and record["sender"]
                            == plan.endpoints[target]["expected_sender"]
                        )
                    )
            item = durable.result()
            receipts = {
                target: native_receipt(peers[target], plan.endpoints[target], marker)
                for target in targets
            }
            transport = plan.endpoints[source]["transport"]
            if transport in {"matrix", "meshtastic", "lxmf"}:
                ref = item["event"]["source_native_ref"]
                assert (
                    ref and ref["native_message_id"] == sent["native_id"]
                ), "Native ingress correlation differs"
            report["cases"].append(
                {
                    "source": source,
                    "marker": marker,
                    "native_send": sent,
                    "core": item,
                    "native_receipts": receipts,
                }
            )
            report["completed_pairs"].extend((source, target) for target in targets)
            write_report(directory, report)
            print(f"PASS {source} -> {', '.join(targets)}", flush=True)
            await asyncio.sleep(plan.pacing)  # Explicit on-air budget between turns.
        await asyncio.sleep(
            plan.settle
        )  # Observe late echoes/duplicates after the final turn.
        final = await snapshots(app.storage, prefix)
        assert len(final) == len(peers), "Unexpected canonical replay or bridge echo"
        for case in report["cases"]:
            source, marker = case["source"], case["marker"]
            items = [item for item in final if marker in str(item["event"]["payload"])]
            assert len(items) == 1, "Missing or duplicate canonical nonce"
            targets = {
                name: endpoint["channel"]
                for name, endpoint in plan.endpoints.items()
                if name != source
            }
            verify(
                items[0],
                source=source,
                channel=plan.endpoints[source]["channel"],
                targets=targets,
            )
            case["core"] = items[0]
            for target in targets:
                native_receipt(peers[target], plan.endpoints[target], marker)
        assert set(map(tuple, report["completed_pairs"])) == set(plan.pairs)
        for peer in peers.values():
            peer.check()
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        # Detailed SDK/loader diagnostics stay under the private run directory.
        (directory / "failure.txt").write_text(traceback.format_exc())
    finally:
        cleanup_errors = []
        if app is not None:
            try:
                await asyncio.wait_for(app.stop(), 45)
                if app.state.value != "stopped":
                    raise RuntimeError("Runtime failed to stop")
            except BaseException:
                cleanup_errors.append("runtime")
        for name, peer in peers.items():
            try:
                await peer.stop()
            except BaseException:
                cleanup_errors.append(name)
        report["cleanup_errors"] = cleanup_errors
        report["status"] = "failed" if failure or cleanup_errors else "passed"
        write_report(directory, report)
    if failure:
        raise failure
    if cleanup_errors:
        raise RuntimeError("Campaign cleanup failed")
    print(
        f"PASS {report['coverage']} campaign: {len(plan.pairs)} directed pairs; evidence: {directory}"
    )
    return report
