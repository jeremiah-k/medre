"""One bounded native ingress per adapter, fanout to every other adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import traceback
from html.parser import HTMLParser
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


def native_expectation(endpoint: dict) -> dict[str, str]:
    """Capture expected native identity from the fixture, not the outbox."""
    channel = endpoint["peer"].get("channel", endpoint["channel"])
    if endpoint["transport"] == "lxmf":
        channel = endpoint["expected_sender"]
    return {
        "transport": endpoint["transport"],
        "source_channel": endpoint["channel"],
        "receipt_sender": endpoint["expected_sender"],
        "receipt_channel": str(channel),
    }


def native_receipt(
    peer: Peer,
    endpoint: dict,
    marker: str,
    *,
    source: str | None = None,
    message: str | None = None,
    max_text_bytes: int | None = None,
) -> dict:
    expected = native_expectation(endpoint)
    hits = [
        record
        for record in peer.records
        if record["kind"] == "received"
        and marker in record["text"]
        and record["sender"] == expected["receipt_sender"]
    ]
    assert len(hits) == 1, f"{peer.name}: missing or duplicate native delivery"
    assert (
        hits[0]["channel"] == expected["receipt_channel"]
    ), f"{peer.name}: incorrect native conversation"
    assert "café ✓" in hits[0]["text"], f"{peer.name}: UTF-8 body was altered"
    assert "\ufffd" not in hits[0]["text"], f"{peer.name}: invalid UTF-8 rendering"
    assert hits[0]["text"].count(marker) == 1, f"{peer.name}: duplicated rendered body"
    record = hits[0]
    presented = record["text"]
    if endpoint["transport"] == "meshcore":
        wire_header = endpoint["expected_sender"] + ": "
        assert presented.startswith(
            wire_header
        ), f"{peer.name}: native sender header differs"
        presented = presented[len(wire_header) :]
    if max_text_bytes is not None:
        assert (
            len(presented.encode("utf-8")) <= max_text_bytes
        ), f"{peer.name}: radio byte budget exceeded"
    if source in endpoint.get("expected_prefixes", {}):
        assert (
            message is not None
        ), "Presentation verification requires the sent message"
        expected = endpoint["expected_prefixes"][source] + message
        if max_text_bytes is not None:
            expected = expected.encode("utf-8")[:max_text_bytes].decode(
                "utf-8", errors="ignore"
            )
        assert (
            presented == expected
        ), f"{peer.name}: rendered prefix or body differs from the presentation contract"
    if record.get("formatted_body") is not None:
        parser = _VisibleText()
        parser.feed(record["formatted_body"])
        parser.close()
        assert (
            "".join(parser.parts) == record["text"]
        ), f"{peer.name}: HTML attribution/body differs from plain text"
    return record


class _VisibleText(HTMLParser):
    """Extract displayed text, including HTML paragraph and line boundaries."""

    _BLOCKS = frozenset({"p", "div", "blockquote", "pre", "li", "ul", "ol"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._pending_break = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
            self._pending_break = False
        elif tag in self._BLOCKS and self.parts:
            self._pending_break = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCKS and self.parts:
            self._pending_break = True

    def handle_data(self, data: str) -> None:
        if data and self._pending_break and self.parts:
            if not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
        self._pending_break = False
        self.parts.append(data)


async def _radio_budget(app: object, target: str, endpoint: dict) -> int | None:
    """Use the actual target's advertised on-wire text capacity."""
    if endpoint["transport"] not in {"meshtastic", "meshcore"}:
        return None
    info = await app.adapters[target].health_check()
    return info.capabilities.max_text_bytes


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
        # Retain fixture-derived expectations separately from the delivery rows
        # under audit. Never infer trusted destinations from the outbox itself.
        "native_expectations": {
            name: native_expectation(endpoint)
            for name, endpoint in plan.endpoints.items()
        },
        "presentation_expectations": {
            name: endpoint.get("expected_prefixes", {})
            for name, endpoint in plan.endpoints.items()
        },
        "python": platform.python_version(),
    }
    write_report(directory, report)
    peers = {name: Peer(name, directory, plan.timeout) for name in plan.endpoints}
    app = None
    failure = None
    try:
        report.update(
            {
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
        )
        write_report(directory, report)
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
            # The trailing space keeps one source's marker from matching
            # inside another's when one endpoint name prefixes another.
            marker = prefix + source + " "
            text = marker + "café ✓"
            if plan.endpoints[source]["transport"] == "matrix":
                # One ordinary and one byte-constrained target observe the same
                # long body. Do not generate oversized ingress on a radio.
                text += " " + "🌍é" * 64
            sent = await peer.send(text)
            report["active_case"] = {
                "source": source,
                "marker": marker,
                "stage": "delivery_verification",
            }
            write_report(directory, report)
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

            receipts = {}
            for target in targets:
                endpoint = plan.endpoints[target]
                budget = await _radio_budget(app, target, endpoint)
                receipts[target] = native_receipt(
                    peers[target],
                    endpoint,
                    marker,
                    source=source,
                    message=text,
                    max_text_bytes=budget,
                )

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
                    "sent_text": text,
                    "native_send": sent,
                    "core": item,
                    "native_receipts": receipts,
                }
            )
            report["completed_pairs"].extend((source, target) for target in targets)
            report.pop("active_case", None)
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
                endpoint = plan.endpoints[target]
                budget = await _radio_budget(app, target, endpoint)
                native_receipt(
                    peers[target],
                    endpoint,
                    marker,
                    source=source,
                    message=case["sent_text"],
                    max_text_bytes=budget,
                )
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
