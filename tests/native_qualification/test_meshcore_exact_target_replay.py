# medre-test-tier: compatibility
"""Real SDK replay to the pinned companion endpoint; no RF recipient claim."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import msgspec
import pytest

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore import adapter as adapter_module
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.compat import HAS_MESHCORE
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.pipeline.retry_plan import OutboxDispatchContext
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
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
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from scripts.ci.native_qualification import build_record, observation_summary, sha256
from tests.helpers.async_utils import wait_until

pytestmark = [
    pytest.mark.local_integration,
    pytest.mark.meshcore_sdk,
    pytest.mark.skipif(not HAS_MESHCORE, reason="requires the pinned MeshCore SDK"),
]

_ROOT = Path(__file__).resolve().parents[2]
_NATIVE = "meshcore-native-proof"
_MONITOR = "meshcore-proof-monitor"
_SOURCE = "native-proof-source"
_EVENT = "meshcore-native-replay"


def _provider_checkout() -> Path:
    return Path(
        os.environ.get("MEDRE_MESHCORE_PROVIDER_ROOT", _ROOT / ".adapter")
    ).resolve()


def _provider_row():
    rows = json.loads((_ROOT / ".github/adapter-compatibility.json").read_text())
    selected = [row for row in rows if row["transport"] == "meshcore"]
    assert len(selected) == 1
    return selected[0]


@pytest.fixture
def companion_class(monkeypatch: pytest.MonkeyPatch):
    """Reuse the provider-owned endpoint from the exact installed manifest pin."""
    provider = _provider_row()
    checkout = _provider_checkout()
    actual = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    assert actual == provider["ref"], "companion fixture must match the manifest pin"
    assert Path(adapter_module.__file__).resolve().is_relative_to(checkout)
    monkeypatch.syspath_prepend(str(checkout / "tests"))
    from medre_meshcore_tests.helpers.meshcore_local_node import LocalMeshCoreNode

    return LocalMeshCoreNode


@dataclass
class NativeCase:
    storage: SQLiteStorage
    runner: PipelineRunner
    adapter: MeshCoreAdapter
    monitor: FakeMeshCoreAdapter
    selection: ExactTargetSelection
    body: str

    async def replay(self, *, selection: ExactTargetSelection | None = None):
        return [
            result
            async for result in ReplayEngine(
                storage=self.storage, pipeline=self.runner
            ).replay(
                ReplayRequest(
                    mode=ReplayMode.BEST_EFFORT,
                    correlation_ids=[_EVENT],
                    exact_target_selections=(selection or self.selection,),
                )
            )
        ]


@asynccontextmanager
async def _case(
    node,
    path: Path,
    *,
    multipart: bool = False,
    seed: bool = True,
    seed_selected: bool = True,
    command_timeout: float | None = None,
):
    config = MeshCoreConfig(
        adapter_id=_NATIVE,
        connection_type="tcp",
        host=node.host,
        port=node.port,
        message_delay_seconds=0,
        max_text_bytes=48 if multipart else 160,
        max_text_parts=8 if multipart else 1,
    ).validate()
    adapter = MeshCoreAdapter(config)
    monitor = FakeMeshCoreAdapter(adapter_id=_MONITOR)
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    rendering = RenderingPipeline()
    rendering.register(MeshCoreRenderer(configs={_NATIVE: config}), priority=50)
    rendering.register(TextRenderer(), priority=100)
    target = RouteTarget(adapter=_NATIVE, channel="2")
    route = Route(
        id="native-proof-route",
        source=RouteSource(
            adapter=_SOURCE, channel=None, event_kinds=("message.created",)
        ),
        targets=[
            target,
            RouteTarget(adapter=_NATIVE, channel="3"),
            RouteTarget(adapter=_MONITOR),
        ],
    )
    plan_id = stable_delivery_plan_id(_EVENT, target, route_id=route.id, target_index=0)
    selection = ExactTargetSelection(
        event_id=_EVENT,
        delivery_plan_id=plan_id,
        target_adapter=_NATIVE,
        target_channel="2",
    )
    runner = PipelineRunner(
        PipelineConfig(
            storage=storage,
            router=Router(routes=[route]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),
            adapters={_NATIVE: adapter, _MONITOR: monitor},
            rendering_pipeline=rendering,
            runtime_accounting=RuntimeAccounting(),
            event_bus=EventBus(),
        )
    )
    body = "MeshCore replay proof " + (
        "bounded native packet " * 4 if multipart else "single packet"
    )
    case = NativeCase(storage, runner, adapter, monitor, selection, body)
    try:
        if seed:
            await storage.append(
                CanonicalEvent(
                    event_id=_EVENT,
                    event_kind="message.created",
                    schema_version=1,
                    timestamp=datetime.now(UTC),
                    source_adapter=_SOURCE,
                    source_transport_id="proof",
                    source_channel_id=None,
                    parent_event_id=None,
                    lineage=(),
                    relations=(),
                    payload={"text": body},
                    metadata=EventMetadata(),
                )
            )
            for index, sibling in enumerate(route.targets):
                if index == 0 and not seed_selected:
                    continue
                sibling_plan = DeliveryPlan(
                    plan_id=stable_delivery_plan_id(
                        _EVENT, sibling, route_id=route.id, target_index=index
                    ),
                    event_id=_EVENT,
                    target=sibling,
                    primary_strategy=DeliveryStrategy(method="text"),
                    route_id=route.id,
                )
                await storage.create_outbox_item(
                    DeliveryOutboxItem(
                        outbox_id=f"staged-{index}",
                        event_id=_EVENT,
                        route_id=route.id,
                        delivery_plan_id=sibling_plan.plan_id,
                        target_adapter=sibling.adapter,
                        target_channel=sibling.channel,
                        status="pending",
                        attempt_number=0,
                    ),
                    dispatch_snapshot=OutboxDispatchSnapshotWrite(
                        kind="staged",
                        context=msgspec.to_builtins(
                            OutboxDispatchContext(
                                route=route,
                                plan=sibling_plan,
                                routing=RoutingMetadata(
                                    matched_routes=(route.id,), route_trace=(route.id,)
                                ),
                            )
                        ),
                    ),
                )
        await runner.start()
        for current in (adapter, monitor):
            await current.start(
                AdapterContext(
                    adapter_id=current.adapter_id,
                    publish_inbound=runner.handle_ingress,
                    logger=logging.getLogger("meshcore.native.proof"),
                    clock=lambda: datetime.now(UTC),
                    shutdown_event=asyncio.Event(),
                )
            )
        # Shorten only fault scenarios through the real SDK's supported budget.
        if command_timeout is not None:
            adapter._session._meshcore.default_timeout = command_timeout
        yield case
    finally:
        try:
            await adapter.stop()
        finally:
            try:
                await monitor.stop()
            finally:
                try:
                    await runner.stop()
                finally:
                    await storage.close()


@pytest.mark.parametrize("multipart", [False, True])
async def test_core_replay_companion_acceptance(
    companion_class, tmp_path: Path, multipart: bool
):
    async with (
        companion_class() as node,
        _case(node, tmp_path / "core.sqlite", multipart=multipart) as case,
    ):
        preview = await preview_exact_target(case.storage, case.selection)
        assert preview.category == "pending"
        assert preview.excluded_sibling_count == 2
        results = await case.replay()
        delivered = [result for result in results if result.stage == "deliver"]
        assert len(delivered) == 1
        assert delivered[0].status == "passed", delivered[0].error
        outcomes = delivered[0].output["adapter_results"]
        assert len(outcomes) == 1
        receipt = outcomes[0].receipt
        assert receipt is not None and receipt.source == "replay"
        assert receipt.status == "sent"
        assert receipt.target_channel == "2" and receipt.attempt_number >= 1
        assert (
            receipt.adapter_message_id is None
        ), "channel OK assigns no native packet ID"
        assert len(await case.storage.list_receipts_for_event(_EVENT)) == 1
        assert not case.monitor.delivered_payloads
        assert node.send_commands
        # The endpoint witnesses real SDK frames independently of the receipt.
        assert all(
            command[0] == 0x03 and command[2] == 2 for command in node.send_commands
        )
        wire_texts = [command[7:].decode("utf-8") for command in node.send_commands]
        if multipart:
            checkpoint = await case.storage.get_radio_attempt(
                receipt.outbox_id, receipt.attempt_number
            )
            assert checkpoint is not None and checkpoint.progress.handoff_complete
            assert wire_texts == [
                part.wire_text for part in checkpoint.progress.plan.parts
            ]
            assert len(wire_texts) > 1
        else:
            assert wire_texts == [case.body]
        provider = _provider_row()
        evidence = NativeEvidenceObservation(
            event_id=receipt.event_id,
            delivery_plan_id=receipt.delivery_plan_id,
            target_adapter=receipt.target_adapter,
            target_channel=receipt.target_channel,
            outbox_id=receipt.outbox_id,
            attempt_number=receipt.attempt_number,
            observed_row_updated_at=None,
            provider="meshcore",
            compatibility_pin=provider["ref"],
            provider_repository=provider["repository"],
            observation_source="independent_companion_protocol_endpoint",
            tier="local_integration",
            scope="provider_acceptance",
            observed_at=datetime.now(UTC),
            result="accepted",
            receipt_id=receipt.receipt_id,
        )
        binding = await assess_native_evidence_ledger_binding(case.storage, evidence)
        assert (binding.state, binding.reason) == ("current", "exact")
    # Cleanup exceptions prevent publication, even after a successful command.
    checkout = _provider_checkout()
    record = build_record(
        _ROOT,
        checkout,
        transport="meshcore",
        observation=observation_summary(evidence),
        binding=(binding.state, binding.reason),
        observer_kind="companion_protocol",
        witness_hashes=[sha256(command) for command in node.send_commands],
        harnesses=[
            ("core", "tests/native_qualification/test_meshcore_exact_target_replay.py"),
            ("provider", "tests/medre_meshcore_tests/helpers/meshcore_local_node.py"),
        ],
    )
    (tmp_path / "qualification.json").write_text(json.dumps(record, indent=2) + "\n")


@pytest.mark.parametrize("fault", ["timeout", "disconnect", "cancel"])
@pytest.mark.parametrize("accepted_prefix", [False, True])
async def test_uncertain_packet_is_not_replayed_after_restart(
    companion_class, tmp_path: Path, fault: str, accepted_prefix: bool
):
    def assert_held(results):
        held = next(result for result in results if result.stage == "deliver")
        if fault == "cancel":
            # Cancellation leaves ownership unresolved: selection refuses the
            # ambiguous live lineage before it can reach a packet dispatch.
            assert held.status == "skipped"
            assert held.output["exact_target_refusals"][0]["category"] == "ambiguous"
        else:
            outcome = held.output["adapter_results"][0]
            assert outcome.status == "permanent_failure"
            assert "already bound" in outcome.error

    class PrefixCompanion(companion_class):
        async def _respond(self, writer, payload):
            if payload == b"\x00" and len(self.send_commands) > 1:
                await self.release_sends.wait()
            await super()._respond(writer, payload)

    companion = (
        PrefixCompanion() if accepted_prefix else companion_class(hold_sends=True)
    )
    expected_calls = 2 if accepted_prefix else 1
    async with companion as node:
        path = tmp_path / "core.sqlite"
        async with _case(node, path, multipart=True, command_timeout=0.2) as case:
            running = asyncio.create_task(case.replay())
            await asyncio.wait_for(node.next_send_receipt(), 3)
            if accepted_prefix:
                await asyncio.wait_for(node.next_send_receipt(), 3)
            if fault == "cancel":
                running.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await running
            else:
                if fault == "disconnect":
                    await node.disconnect_clients()
                await running
            assert (
                len(node.send_commands) == expected_calls
            ), "guarded SDK calls must not retry uncertainty"
            rows = await case.storage.list_outbox_items_for_delivery(
                case.selection.identity
            )
            checkpoints = [
                await case.storage.get_latest_radio_attempt(
                    row.outbox_id,
                    before_attempt=max(row.attempt_number, row.active_attempt or 0) + 1,
                )
                for row in rows
            ]
            retained = next(
                checkpoint for checkpoint in checkpoints if checkpoint is not None
            )
            states = [part.state for part in retained.progress.parts]
            assert "unknown" in states or "submitting" in states
            if accepted_prefix:
                assert states[0] == "accepted"
            assert_held(await case.replay())
            assert len(node.send_commands) == expected_calls
        async with _case(node, path, multipart=True, seed=False) as restarted:
            assert_held(await restarted.replay())
            assert (
                len(node.send_commands) == expected_calls
            ), "restart must not erase uncertain packet ownership"
            assert not restarted.monitor.delivered_payloads


@pytest.mark.parametrize("refusal", ["missing_generation", "wrong_channel"])
async def test_unbound_target_stages_no_native_command(
    companion_class, tmp_path: Path, refusal: str
):
    async with (
        companion_class() as node,
        _case(
            node,
            tmp_path / "core.sqlite",
            seed_selected=refusal != "missing_generation",
        ) as case,
    ):
        selection = ExactTargetSelection(
            event_id=_EVENT,
            delivery_plan_id=case.selection.delivery_plan_id,
            target_adapter=_NATIVE,
            target_channel="9" if refusal == "wrong_channel" else "2",
        )
        results = await case.replay(selection=selection)
        delivered = next(result for result in results if result.stage == "deliver")
        assert delivered.status == "skipped"
        if refusal == "missing_generation":
            assert "exact_target_refused: ambiguous" in delivered.error
        else:
            assert "No delivery plans matched" in delivered.error
        assert not node.send_commands
        assert not case.monitor.delivered_payloads


async def test_stale_preview_rederives_generation_and_fences_old_worker(
    companion_class, tmp_path: Path
):
    async with companion_class() as node, _case(node, tmp_path / "core.sqlite") as case:
        before = await preview_exact_target(case.storage, case.selection)
        assert before.category == "pending"
        await case.storage.claim_due_outbox_items(
            datetime.now(UTC).isoformat(), "competing-worker"
        )
        assert (
            await case.storage.reserve_outbox_attempt("staged-0", "competing-worker", 0)
            == 1
        )
        stale = await preview_exact_target(
            case.storage,
            case.selection,
            expected_fingerprint=before.observed_fingerprint,
            expected_generation=before.observed_generation,
        )
        assert stale.category == "stale"
        # Exact selection carries identity, not an old preview's authority.
        # The contract permits fresh admission to supersede an unsubmitted row.
        results = await case.replay()
        delivered = next(result for result in results if result.stage == "deliver")
        assert delivered.status == "passed", delivered.error
        receipt = delivered.output["adapter_results"][0].receipt
        assert receipt.attempt_number == 2 and receipt.outbox_id != "staged-0"
        assert len(node.send_commands) == 1
        assert not await case.storage.authorize_native_submission(
            _EVENT, "staged-0", worker_id="competing-worker", attempt_number=1
        )


async def test_reconnect_before_replay_keeps_one_native_submission(
    companion_class, tmp_path: Path
):
    async with companion_class() as node, _case(node, tmp_path / "core.sqlite") as case:
        await node.disconnect_clients()
        assert await wait_until(
            lambda: node.connection_count >= 2 and case.adapter._session.connected,
            timeout=5,
        )
        results = await case.replay()
        assert any(
            result.stage == "deliver" and result.status == "passed"
            for result in results
        )
        assert len(node.send_commands) == 1
        assert not case.monitor.delivered_payloads
