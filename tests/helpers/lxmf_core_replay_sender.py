# medre-test-tier: compatibility
"""Owned LXMF native replay sender for the POSIX local-integration scenario.

The receiver runs a separate, raw-SDK Reticulum instance. The sender uses the
real MEDRE pipeline and a real LXMF adapter with its owned native worker.
The script is launched only by the gated integration test in an owned group.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import msgspec

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.pipeline.retry_plan import OutboxDispatchContext
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.events.canonical import DeliveryReceipt
from medre.core.events.metadata import RoutingMetadata
from medre.core.evidence.native_evidence import NativeEvidenceObservation
from medre.core.evidence.native_ledger_binding import (
    assess_native_evidence_ledger_binding,
)
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    stable_delivery_plan_id,
)
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    StorageBackend,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting

_ADAPTER_ID = "lxmf-exact-target-native"
_MONITOR_ID = "lxmf-exact-target-monitor"
_SOURCE_ID = "lxmf-native-ci-source"
_ROUTE_ID = "lxmf-native-exact-route"
_RESULT_PREFIX = "MEDRE_LXMF_CORE_REPLAY_RESULT="


def _write_config(home: Path, name: str, listen: int, peer: int) -> None:
    target = home / ".reticulum"
    target.mkdir(parents=True, exist_ok=True)
    (target / "config").write_text(
        "[reticulum]\nenable_transport = No\nshare_instance = No\n\n"
        "[logging]\nloglevel = 4\n\n[interfaces]\n"
        f"  [[{name}]]\n    type = UDPInterface\n    enabled = Yes\n"
        f"    listen_ip = 127.0.0.1\n    listen_port = {listen}\n"
        f"    forward_ip = 127.0.0.1\n    forward_port = {peer}\n",
        encoding="utf-8",
    )


def _ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as a,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as b,
    ):
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return int(a.getsockname()[1]), int(b.getsockname()[1])


async def _await_json(path: Path, timeout: float) -> dict[str, Any]:
    # This module also runs as a standalone owned-group script, where the
    # repository root is not on sys.path; bootstrap it so the sanctioned
    # polling helper is importable in that subprocess context.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from tests.helpers.async_utils import wait_until

    value: dict[str, Any] | None = None

    def _published() -> bool:
        nonlocal value
        if path.is_file():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if isinstance(parsed, dict):
                value = parsed
                return True
        return False

    if not await wait_until(_published, timeout=timeout):
        raise AssertionError(
            f"native peer did not publish {path.name} within {timeout}s"
        )
    assert value is not None
    return value


def _event(event_id: str, body: str) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter=_SOURCE_ID,
        source_transport_id="core-replay-ci-source",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": body, "title": "medre lxmf core replay"},
        metadata=EventMetadata(),
    )


def _context(adapter_id: str, runner: PipelineRunner) -> AdapterContext:
    async def publish(event: Any) -> None:
        await runner.handle_ingress(event)

    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=publish,
        logger=logging.getLogger(f"lxmf.core.native.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


def _route(destination_hash: str) -> Route:
    return Route(
        id=_ROUTE_ID,
        source=RouteSource(
            adapter=_SOURCE_ID,
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[
            RouteTarget(adapter=_ADAPTER_ID, channel=destination_hash),
            RouteTarget(adapter=_MONITOR_ID),
        ],
    )


async def _replay_core(base: Path, target: str, propagation: str) -> dict[str, Any]:
    config = LxmfConfig(
        adapter_id=_ADAPTER_ID,
        connection_type="reticulum",
        default_delivery_method="propagated",
        outbound_propagation_node=propagation,
        reticulum_config_dir=str(base / "home-a" / ".reticulum"),
        storage_path=str(base / "a" / "router"),
        announce_interval_seconds=0,
        message_delay_seconds=0,
        metadata_embedding=False,
    ).validate()
    adapter = LxmfAdapter(config)
    monitor = FakeMatrixAdapter(_MONITOR_ID)
    storage = SQLiteStorage(str(base / "core.sqlite"))
    await storage.initialize()
    rendering = RenderingPipeline()
    rendering.register(
        LxmfRenderer(configs={_ADAPTER_ID: config}, metadata_embedding=False),
        priority=50,
    )
    rendering.register(TextRenderer(), priority=100)
    route = _route(target)
    runner = PipelineRunner(
        PipelineConfig(
            storage=cast(StorageBackend, storage),
            router=Router(routes=[route]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),
            adapters={_ADAPTER_ID: adapter, _MONITOR_ID: monitor},
            rendering_pipeline=rendering,
            runtime_accounting=RuntimeAccounting(),
            event_bus=EventBus(),
        )
    )
    nonce = uuid.uuid4().hex
    event_id = f"lxmf-core-replay-{nonce}"
    body = f"medre core replay native body {nonce}"
    plan_id = stable_delivery_plan_id(
        event_id,
        RouteTarget(adapter=_ADAPTER_ID, channel=target),
        route_id=_ROUTE_ID,
        target_index=0,
    )
    plan = DeliveryPlan(
        plan_id=plan_id,
        event_id=event_id,
        target=RouteTarget(adapter=_ADAPTER_ID, channel=target),
        primary_strategy=DeliveryStrategy(method="propagated"),
        route_id=_ROUTE_ID,
    )
    item = DeliveryOutboxItem(
        outbox_id=f"outbox-{uuid.uuid4().hex}",
        event_id=event_id,
        route_id=_ROUTE_ID,
        delivery_plan_id=plan_id,
        target_adapter=_ADAPTER_ID,
        target_channel=target,
        attempt_number=1,
        status="pending",
    )
    selection = ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=_ADAPTER_ID,
        target_channel=target,
    )
    started = False
    try:
        await storage.append(_event(event_id, body))
        await storage.create_outbox_item(
            item,
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="staged",
                context=msgspec.to_builtins(
                    OutboxDispatchContext(
                        route=route,
                        plan=plan,
                        routing=RoutingMetadata(
                            matched_routes=(route.id,),
                            route_trace=(route.id,),
                        ),
                    )
                ),
            ),
        )
        preview = await preview_exact_target(storage, selection)
        assert preview.category == "pending"
        await runner.start()
        started = True
        await adapter.start(_context(_ADAPTER_ID, runner))
        await monitor.start(_context(_MONITOR_ID, runner))
        # The native worker owns RNS. Its send path performs a bounded path
        # discovery before constructing an LXMessage; no test-only direct
        # RNS instance is ever constructed in this parent process.
        engine = ReplayEngine(storage=cast(StorageBackend, storage), pipeline=runner)
        results = [
            result
            async for result in engine.replay(
                ReplayRequest(
                    mode=ReplayMode.BEST_EFFORT,
                    correlation_ids=[event_id],
                    run_id=f"lxmf-native-{nonce}",
                    exact_target_selections=(selection,),
                )
            )
        ]
        delivered = [
            result
            for result in results
            if result.event_id == event_id and result.stage == "deliver"
        ]
        assert len(delivered) == 1
        assert delivered[0].status == "passed", delivered[0].error
        assert delivered[0].output is not None
        outcomes = delivered[0].output["adapter_results"]
        chosen = [x for x in outcomes if x.target_adapter == _ADAPTER_ID]
        assert len(chosen) == 1
        outcome = chosen[0]
        assert outcome.status in {"success", "queued"}, (
            "native handoff was not established",
            outcome.status,
            outcome.error,
        )
        receipt = outcome.receipt
        assert isinstance(receipt, DeliveryReceipt)
        assert receipt.source == "replay" and receipt.outbox_id
        assert receipt.adapter_message_id
        assert receipt.target_channel == target
        assert len(monitor.delivered_payloads) == 0
        received = await _await_json(base / "b-received.json", 90.0)
        assert received.get("received") is True, received
        assert received.get("delivery_count", received.get("deliveries_so_far")) == 1
        assert received.get("destination_hash") == target
        assert received.get("message_hash") == receipt.adapter_message_id
        assert received.get("body") == body
        manifest = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / ".github/adapter-compatibility.json"
            ).read_text(encoding="utf-8")
        )
        pin = next(row["ref"] for row in manifest if row["transport"] == "lxmf")
        evidence = NativeEvidenceObservation(
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter=receipt.target_adapter,
            target_channel=receipt.target_channel,
            outbox_id=receipt.outbox_id,
            attempt_number=receipt.attempt_number,
            observed_row_updated_at=None,
            provider="lxmf",
            provider_repository="jeremiah-k/medre-adapter-lxmf",
            compatibility_pin=pin,
            observation_source="independent_lxmf_receiver_callback",
            tier="local_integration",
            scope="recipient_observation",
            observed_at=datetime.now(UTC),
            result="accepted",
            receipt_id=receipt.receipt_id,
            native_message_id=receipt.adapter_message_id,
            evidence_reference=None,
        )
        binding = await assess_native_evidence_ledger_binding(storage, evidence)
        assert (binding.state, binding.reason) == ("current", "exact"), binding
        assert evidence.resolution == "accepted"
        # Only a bounded, content-free summary is eligible for uploaded CI
        # evidence. Raw recipient payloads stay inside the owned test directory.
        recipient_verdict = {
            "received": True,
            "delivery_count": 1,
            "body_matches": True,
            "native_id_matches": True,
            "destination_matches": True,
            "evidence_fingerprint": evidence.evidence_fingerprint,
        }
        (base / "recipient-verdict.json").write_text(
            json.dumps(recipient_verdict, sort_keys=True), encoding="utf-8"
        )
        return {
            "scenario": "lxmf_core_authorized_exact_target_replay",
            "provider": evidence.provider,
            "tier": evidence.tier,
            "scope": evidence.scope,
            "result": evidence.result,
            "native_evidence_fingerprint": evidence.evidence_fingerprint,
            "binding": binding.state,
            "source": receipt.source,
            "attempt_number": receipt.attempt_number,
            "native_id_matches_receiver": True,
            "destination_matches_receiver": True,
            "sibling_deliveries": len(monitor.delivered_payloads),
            "recipient_callback_observed": True,
            "native_handoff_status": outcome.status,
            "limitations": [
                "Local loopback Reticulum/LXMF only; no external peers or hardware",
                "Independent receiver callback is distinct from native handoff",
                "No uncertain-native-handoff remediation authority is exercised",
            ],
        }
    finally:
        if started:
            try:
                await adapter.stop()
            finally:
                try:
                    await monitor.stop()
                finally:
                    await runner.stop()
        await storage.close()


async def _main(base: Path, receiver: Path) -> None:
    home_a = base / "home-a"
    home_b = base / "home-b"
    port_a, port_b = _ports()
    _write_config(home_a, "medre-core-a", port_a, port_b)
    _write_config(home_b, "medre-core-b", port_b, port_a)
    receiver_log = (base / "receiver.log").open("wb")
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            str(receiver),
            str(base),
            stdout=receiver_log,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "HOME": str(home_b)},
        )
    except BaseException:
        receiver_log.close()
        raise
    try:
        ready = await _await_json(base / "b-ready.json", 60.0)
        assert "error" not in ready, ready
        os.environ["HOME"] = str(home_a)
        verdict = await _replay_core(
            base,
            str(ready["delivery_hash"]),
            str(ready["propagation_hash"]),
        )
    finally:
        (base / "stop").write_text("done", encoding="utf-8")
        try:
            await asyncio.wait_for(child.wait(), 30)
        except TimeoutError:
            child.kill()
            await child.wait()
        receiver_log.close()
    final = await _await_json(base / "b-received.json", 30.0)
    assert final.get("received") is True
    assert final.get("delivery_count") == 1
    print(_RESULT_PREFIX + json.dumps(verdict, sort_keys=True), flush=True)


if __name__ == "__main__":
    asyncio.run(_main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
