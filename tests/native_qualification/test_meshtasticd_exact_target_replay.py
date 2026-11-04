# medre-test-tier: compatibility
"""Core-authorized replay through the pinned Meshtastic SDK and meshtasticd.

The Docker endpoint witnesses a real TCP-backed SDK session; the SDK's return
packet ID is local submission evidence, NOT an independently observed daemon
acceptance, RF recipient delivery, or authorization to retry an uncertain send.
Ordinary messages finalize asynchronously; planned packets consume synchronous
one-use grants. Neither path may broaden an exact selection to its siblings.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import msgspec
import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.pipeline.retry_plan import OutboxDispatchContext
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.events.metadata import RoutingMetadata
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
from tests.helpers.async_utils import wait_until

from tests.integration.conftest import MeshtasticdEnvironment
from tests.integration.conftest import meshtasticd_env as meshtasticd_env

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not HAS_MESHTASTIC, reason="requires pinned mtjk SDK"),
]

_NATIVE = "meshtastic-exact-target"
_MONITOR = "meshtastic-exact-monitor"
_SOURCE = "meshtastic-exact-source"
_ROUTE = "meshtastic-exact-replay"


def _route() -> Route:
    """Keep the selected channel first so its plan identity is deterministic."""
    return Route(
        id=_ROUTE,
        source=RouteSource(
            adapter=_SOURCE, channel=None, event_kinds=("message.created",)
        ),
        targets=[
            RouteTarget(adapter=_NATIVE, channel="0"),
            RouteTarget(adapter=_NATIVE, channel="1"),
            RouteTarget(adapter=_MONITOR),
        ],
    )


def _context(adapter_id: str, runner: PipelineRunner) -> AdapterContext:
    """Wire real deferred feedback into the same runner that owns replay."""
    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=runner.handle_ingress,
        report_delivery_feedback=runner._record_delivery_feedback,
        logger=logging.getLogger("test.meshtastic.exact_target"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


@dataclass
class ReplayCase:
    storage: SQLiteStorage
    runner: PipelineRunner
    radio: MeshtasticAdapter
    monitor: FakeMeshtasticAdapter
    selection: ExactTargetSelection
    event_id: str
    body: str
    sdk_calls: list[tuple[str, int]] = field(default_factory=list)

    async def replay(self, selection: ExactTargetSelection | None = None) -> list[Any]:
        """Request a scoped replay through the durable exact-target authority."""
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            correlation_ids=[self.event_id],
            exact_target_selections=(selection or self.selection,),
        )
        return [
            result
            async for result in ReplayEngine(
                storage=self.storage, pipeline=self.runner
            ).replay(request)
        ]


@asynccontextmanager
async def _case(
    env: MeshtasticdEnvironment,
    path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    multipart: bool = False,
    seed_selected: bool = True,
):
    """Assemble one real daemon session and three staged sibling generations."""
    config = MeshtasticConfig(
        adapter_id=_NATIVE,
        connection_type="tcp",
        host=env.host,
        port=env.port,
        message_delay_seconds=0,
        max_text_bytes=56 if multipart else 230,
        max_text_parts=8 if multipart else 1,
    ).validate()
    radio = MeshtasticAdapter(config)
    monitor = FakeMeshtasticAdapter(adapter_id=_MONITOR)
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    route = _route()
    rendering = RenderingPipeline()
    rendering.register(MeshtasticRenderer(configs={_NATIVE: config}), priority=50)
    rendering.register(TextRenderer(), priority=100)
    runner = PipelineRunner(
        PipelineConfig(
            storage=storage,
            router=Router(routes=[route]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=storage),
            adapters={_NATIVE: radio, _MONITOR: monitor},
            rendering_pipeline=rendering,
            runtime_accounting=RuntimeAccounting(),
            event_bus=EventBus(),
        )
    )
    event_id = "meshtastic-replay-" + uuid.uuid4().hex
    body = "meshtastic exact target " + (
        "planned radio packet " * 7 if multipart else "ordinary packet"
    )
    selected = route.targets[0]
    selection = ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=stable_delivery_plan_id(
            event_id, selected, route_id=route.id, target_index=0
        ),
        target_adapter=_NATIVE,
        target_channel="0",
    )
    case = ReplayCase(storage, runner, radio, monitor, selection, event_id, body)
    try:
        await storage.append(
            CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(UTC),
                source_adapter=_SOURCE,
                source_transport_id="docker-proof",
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
            plan = DeliveryPlan(
                plan_id=stable_delivery_plan_id(
                    event_id, sibling, route_id=route.id, target_index=index
                ),
                event_id=event_id,
                target=sibling,
                primary_strategy=DeliveryStrategy(method="text"),
                route_id=route.id,
            )
            await storage.create_outbox_item(
                DeliveryOutboxItem(
                    outbox_id=f"staged-{index}",
                    event_id=event_id,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
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
                            plan=plan,
                            routing=RoutingMetadata(
                                matched_routes=(route.id,), route_trace=(route.id,)
                            ),
                        )
                    ),
                ),
            )
        await runner.start()
        await radio.start(_context(_NATIVE, runner))
        await monitor.start(_context(_MONITOR, runner))
        # Observe the actual pinned SDK entry without replacing its result or
        # the TCP daemon. This witnesses invocation, never daemon acceptance.
        client = radio._session._client
        assert client is not None
        send_text = client.sendText

        def record_sdk_send(text: str, *, channelIndex: int = 0, **kwargs: Any):
            case.sdk_calls.append((text, channelIndex))
            return send_text(text, channelIndex=channelIndex, **kwargs)

        monkeypatch.setattr(client, "sendText", record_sdk_send)
        yield case
    finally:
        try:
            await radio.stop()
        finally:
            try:
                await monitor.stop()
            finally:
                try:
                    await runner.stop()
                finally:
                    await storage.close()


def _single_outcome(results: list[Any]) -> Any:
    """Require exactly one selected adapter outcome, never a sibling fan-out."""
    delivered = [result for result in results if result.stage == "deliver"]
    assert len(delivered) == 1
    assert delivered[0].status == "passed", delivered[0].error
    outcomes = delivered[0].output["adapter_results"]
    assert len(outcomes) == 1
    assert outcomes[0].target_adapter == _NATIVE
    return outcomes[0]


@pytest.mark.parametrize("multipart", [False, True])
async def test_exact_target_replay_uses_real_daemon_sdk_boundary(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multipart: bool,
) -> None:
    """Exercise one persisted target, the actual SDK queue, and durable feedback.

    No independent RF/daemon observer is available here: the SDK packet ID is
    local handoff evidence only, never a native recipient acknowledgement.
    """
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        monkeypatch=monkeypatch,
        multipart=multipart,
    ) as case:
        preview = await preview_exact_target(case.storage, case.selection)
        assert preview.category == "pending"
        assert preview.excluded_sibling_count == 2
        outcome = _single_outcome(await case.replay())
        assert outcome.status == ("success" if multipart else "queued")
        assert outcome.receipt is not None
        assert outcome.receipt.source == "replay"
        assert outcome.receipt.target_channel == "0"
        assert outcome.receipt.status == ("sent" if multipart else "queued")
        if not multipart:
            assert await wait_until(
                lambda: case.radio.queue.total_sent == 1, timeout=25
            )
        assert case.radio.queue.total_failed == 0
        assert not case.monitor.delivered_payloads
        receipts = await case.storage.list_receipts_for_event(case.event_id)
        assert not any(receipt.target_channel == "1" for receipt in receipts)
        assert not any(receipt.target_adapter == _MONITOR for receipt in receipts)
        if multipart:
            checkpoint = await case.storage.get_radio_attempt(
                outcome.receipt.outbox_id, outcome.receipt.attempt_number
            )
            assert checkpoint is not None
            assert checkpoint.progress.handoff_complete
            assert len(checkpoint.progress.plan.parts) > 1
            assert all(part.state == "accepted" for part in checkpoint.progress.parts)
            assert case.sdk_calls == [
                (part.wire_text, 0) for part in checkpoint.progress.plan.parts
            ]
            assert case.radio.queue.total_sent == len(case.sdk_calls)
        else:
            # Deferred completion is a different durable fact from queue admission.
            async def has_sent_receipt() -> bool:
                rows = await case.storage.list_receipts_for_event(case.event_id)
                return any(receipt.status == "sent" for receipt in rows)

            assert await wait_until(has_sent_receipt, timeout=10)
            finished = await case.storage.list_receipts_for_event(case.event_id)
            sent = [r for r in finished if r.status == "sent"]
            assert len(sent) == 1
            assert sent[0].adapter_message_id is not None
            assert sent[0].target_channel == "0"
            assert sent[0].outbox_id == outcome.receipt.outbox_id
            assert sent[0].attempt_number == outcome.receipt.attempt_number
            assert sent[0].delivery_plan_id == case.selection.delivery_plan_id
            assert case.sdk_calls == [(f"docker-proof: {case.body}", 0)]
        for index in (1, 2):
            sibling = await case.storage.get_outbox_item(f"staged-{index}")
            assert sibling is not None and sibling.status == "pending"
            assert sibling.attempt_number == 0 and sibling.active_attempt is None


@pytest.mark.parametrize("refusal", ["missing_generation", "wrong_channel"])
async def test_exact_target_refusal_does_not_enqueue_or_send(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """Neither a missing generation nor a different target can send on a sibling."""
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        seed_selected=refusal != "missing_generation",
        monkeypatch=monkeypatch,
    ) as case:
        selection = ExactTargetSelection(
            event_id=case.event_id,
            delivery_plan_id=case.selection.delivery_plan_id,
            target_adapter=_NATIVE,
            target_channel="9" if refusal == "wrong_channel" else "0",
        )
        delivered = [r for r in await case.replay(selection) if r.stage == "deliver"]
        assert len(delivered) == 1
        assert delivered[0].status == "skipped"
        expected = (
            "exact_target_refused: ambiguous"
            if refusal == "missing_generation"
            else "No delivery plans matched"
        )
        assert expected in delivered[0].error
        assert not case.sdk_calls
        assert case.radio.queue.total_sent == 0
        assert case.radio.queue.pending_count == 0
        assert not case.monitor.delivered_payloads
        assert not await case.storage.list_receipts_for_event(case.event_id)


async def test_stale_preview_rederives_generation_and_fences_old_worker(
    meshtasticd_env: MeshtasticdEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed-but-unsubmitted stale generation cannot authorize SDK use."""
    async with _case(
        meshtasticd_env,
        tmp_path / "medre.sqlite",
        multipart=True,
        monkeypatch=monkeypatch,
    ) as case:
        before = await preview_exact_target(case.storage, case.selection)
        assert before.category == "pending"
        claims = await case.storage.claim_due_outbox_items(
            datetime.now(UTC).isoformat(), "competing-worker", limit=1
        )
        assert [row.outbox_id for row in claims] == ["staged-0"]
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
        loop = asyncio.get_running_loop()
        client = case.radio._session._client
        sdk_send = client.sendText

        def require_old_worker_fenced(text: str, **kwargs: Any):
            # This runs in the SDK worker. Observe durable refusal before the
            # actual SDK invocation, not merely after the replay has finished.
            allowed = asyncio.run_coroutine_threadsafe(
                case.storage.authorize_native_submission(
                    case.event_id,
                    "staged-0",
                    worker_id="competing-worker",
                    attempt_number=1,
                ),
                loop,
            ).result(timeout=5)
            assert not allowed
            return sdk_send(text, **kwargs)

        monkeypatch.setattr(client, "sendText", require_old_worker_fenced)
        outcome = _single_outcome(await case.replay())
        assert outcome.status == "success"
        assert outcome.receipt is not None
        assert outcome.receipt.outbox_id != "staged-0"
        assert outcome.receipt.attempt_number == 2
        checkpoint = await case.storage.get_radio_attempt(
            outcome.receipt.outbox_id, outcome.receipt.attempt_number
        )
        assert checkpoint is not None and checkpoint.progress.handoff_complete
        assert case.sdk_calls == [
            (part.wire_text, 0) for part in checkpoint.progress.plan.parts
        ]
        assert case.radio.queue.total_sent == len(case.sdk_calls) > 1
        assert not await case.storage.authorize_native_submission(
            case.event_id,
            "staged-0",
            worker_id="competing-worker",
            attempt_number=1,
        )
        assert not case.monitor.delivered_payloads
