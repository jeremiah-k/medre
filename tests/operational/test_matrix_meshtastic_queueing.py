"""Matrix <-> Meshtastic queueing, receipt, and correlation operational tests.

Tests covering:
- Queued delivery receipt creation through TargetDeliveryService
- delivery_plan_id correlation for queued-to-sent receipt pairing
- Ambiguity handling when correlation is under-specified
- Queue backpressure and capacity rejection
- Queue health diagnostics
- Delivery state validation (receipt status transitions)

All tests use fakes -- no real Matrix homeserver, no real Meshtastic radio.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.meshtastic.errors import MeshtasticSendError
from medre.adapters.meshtastic.queue import MeshtasticOutboundQueue
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.core.contracts.adapter import (
    OutboundNativeRefRecord,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events.canonical import (
    DeliveryReceipt,
)
from medre.core.rendering.renderer import (
    RenderingResult,
)
from medre.core.routing.models import RouteSource

# Reuse helpers from the flow module.
from tests.operational.test_matrix_meshtastic_flow import (
    _FakeStorage,
    _make_meshtastic_config,
    _matrix_inbound_event,
)

# ===========================================================================
# E. Queued receipt creation through DeliveryLifecycleService
# ===========================================================================


class TestQueuedReceiptCreation:
    """Queued delivery creates a queued receipt with plan_id and evidence."""

    @pytest.mark.asyncio
    async def test_queued_adapter_creates_queued_receipt_with_plan_id(self) -> None:
        """Deliver through TargetDeliveryService with a queue-returning adapter
        and assert a queued receipt with delivery_plan_id and evidence."""
        from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.routing.models import Route, RouteTarget

        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))
        diagnostician = Diagnostician()

        # Build an adapter that returns delivery_status="enqueued" to simulate
        # a queue-based adapter (like MeshtasticOutboundQueue).
        config = _make_meshtastic_config()
        mesh_adapter = FakeMeshtasticAdapter(config)

        # Patch deliver to return enqueued status.
        _enqueued_calls: list[RenderingResult] = []

        async def _enqueued_deliver(
            result: RenderingResult,
        ) -> Any:
            from types import MappingProxyType

            from medre.core.contracts.adapter import AdapterDeliveryResult

            _enqueued_calls.append(result)
            # Return result with enqueued status to trigger queued receipt.
            return AdapterDeliveryResult(
                native_message_id=None,
                native_channel_id=str(result.payload.get("channel_index", 0)),
                metadata=MappingProxyType({"adapter_status": "enqueued"}),
                delivery_status="enqueued",
            )

        mesh_adapter.deliver = _enqueued_deliver  # type: ignore[assignment]

        rendering_pipeline = RenderingPipeline()
        rendering_pipeline.register(
            MeshtasticRenderer(configs={"test_mesh": config}), priority=10
        )
        rendering_pipeline.register_platforms_from({"test_mesh": "meshtastic"})

        svc = TargetDeliveryService(
            adapters={"test_mesh": mesh_adapter},
            rendering_pipeline=rendering_pipeline,
            storage=storage,
            diagnostician=diagnostician,
            lifecycle=lifecycle,
            logger=logging.getLogger("test"),
        )

        event = _matrix_inbound_event(body="Queued msg")
        route = Route(
            id="route-q",
            source=RouteSource(adapter="test_matrix", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="test_mesh", channel="0")],
        )
        plan = DeliveryPlan(
            plan_id="plan-q-1",
            event_id=event.event_id,
            target=RouteTarget(adapter="test_mesh", channel="0"),
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt is not None
        assert receipt.status == "queued"
        assert receipt.delivery_plan_id == "plan-q-1"
        assert receipt.target_adapter == "test_mesh"
        assert receipt.target_channel == "0"
        assert receipt.route_id == "route-q"
        assert receipt.rendering_evidence is not None
        assert receipt.attempt_number == 1

        # Adapter was called with the rendered result.
        assert len(_enqueued_calls) == 1

        # Receipt persisted in storage.
        stored = await storage.list_receipts_for_event(event.event_id)
        queued = [r for r in stored if r.status == "queued"]
        assert len(queued) == 1

    @pytest.mark.asyncio
    async def test_direct_adapter_creates_sent_receipt(self) -> None:
        """Direct (synchronous) adapter delivery creates a sent receipt."""
        from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.routing.models import Route, RouteTarget

        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))
        diagnostician = Diagnostician()

        config = _make_meshtastic_config()
        mesh_adapter = FakeMeshtasticAdapter(config)

        rendering_pipeline = RenderingPipeline()
        rendering_pipeline.register(
            MeshtasticRenderer(configs={"test_mesh": config}), priority=10
        )
        rendering_pipeline.register_platforms_from({"test_mesh": "meshtastic"})

        svc = TargetDeliveryService(
            adapters={"test_mesh": mesh_adapter},
            rendering_pipeline=rendering_pipeline,
            storage=storage,
            diagnostician=diagnostician,
            lifecycle=lifecycle,
            logger=logging.getLogger("test"),
        )

        event = _matrix_inbound_event(body="Direct msg")
        route = Route(
            id="route-d",
            source=RouteSource(adapter="test_matrix", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="test_mesh", channel="0")],
        )
        plan = DeliveryPlan(
            plan_id="plan-d-1",
            event_id=event.event_id,
            target=RouteTarget(adapter="test_mesh", channel="0"),
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt is not None
        assert receipt.status == "sent"
        assert receipt.delivery_plan_id == "plan-d-1"
        assert receipt.rendering_evidence is not None
        assert receipt.adapter_message_id is not None


# ===========================================================================
# F. Queued -> sent correlation
# ===========================================================================


class TestQueuedSentCorrelation:
    """delivery_plan_id correlates queued receipts to supplemental sent
    receipts via DeliveryLifecycleService.append_queued_to_sent_receipt."""

    @pytest.mark.asyncio
    async def test_exact_plan_id_and_channel_finds_queued_receipt(self) -> None:
        """Exact delivery_plan_id + channel finds the correct queued receipt
        and appends a supplemental sent receipt."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        plan_id = str(uuid.uuid4())
        queued_receipt = DeliveryReceipt(
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id="evt-1",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        await storage.append_receipt(queued_receipt)

        record = OutboundNativeRefRecord(
            event_id="evt-1",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="42",
            delivery_plan_id=plan_id,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )

        # Queued receipt still exists.
        # Supplemental sent receipt was appended.
        all_receipts = storage._receipts
        sent_receipts = [r for r in all_receipts if r.status == "sent"]
        assert len(sent_receipts) == 1

        sent = sent_receipts[0]
        assert sent.delivery_plan_id == plan_id
        assert sent.parent_receipt_id == queued_receipt.receipt_id
        assert sent.route_id == "route-1"
        assert sent.target_channel == "0"
        assert sent.adapter_message_id == "42"
        # Evidence inherited from queued receipt.
        assert sent.rendering_evidence == queued_receipt.rendering_evidence

    @pytest.mark.asyncio
    async def test_supplemental_sent_preserves_plan_and_route(self) -> None:
        """Supplemental sent receipt preserves delivery_plan_id,
        parent_receipt_id, route_id, target_channel, and evidence."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        plan_id = str(uuid.uuid4())
        evidence = '{"renderer":"meshtastic","target_platform":"meshtastic"}'
        queued = DeliveryReceipt(
            receipt_id="rcpt-q1",
            event_id="evt-2",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="1",
            route_id="route-x",
            status="queued",
            created_at=datetime.now(timezone.utc),
            rendering_evidence=evidence,
        )
        await storage.append_receipt(queued)

        record = OutboundNativeRefRecord(
            event_id="evt-2",
            adapter="test_mesh",
            native_channel_id="1",
            native_message_id="99",
            delivery_plan_id=plan_id,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )

        sent = [r for r in storage._receipts if r.status == "sent"][0]
        assert sent.delivery_plan_id == plan_id
        assert sent.parent_receipt_id == "rcpt-q1"
        assert sent.route_id == "route-x"
        assert sent.target_channel == "1"
        assert sent.rendering_evidence == evidence

    @pytest.mark.asyncio
    async def test_multiple_queued_same_plan_same_channel_latest_wins(self) -> None:
        """Multiple queued receipts under same plan + channel: latest
        (last appended) wins for retry lineage."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        plan_id = str(uuid.uuid4())
        queued1 = DeliveryReceipt(
            receipt_id="rcpt-q-first",
            event_id="evt-3",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
            attempt_number=1,
        )
        queued2 = DeliveryReceipt(
            receipt_id="rcpt-q-retry",
            event_id="evt-3",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
            attempt_number=2,
        )
        await storage.append_receipt(queued1)
        await storage.append_receipt(queued2)

        record = OutboundNativeRefRecord(
            event_id="evt-3",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="55",
            delivery_plan_id=plan_id,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )

        sent = [r for r in storage._receipts if r.status == "sent"]
        assert len(sent) == 1
        # Latest queued receipt (attempt 2) was used as parent.
        assert sent[0].parent_receipt_id == "rcpt-q-retry"
        assert sent[0].attempt_number == 2

    @pytest.mark.asyncio
    async def test_multiple_queued_different_plans_no_plan_id_warns(self) -> None:
        """Multiple queued receipts with different plans, no delivery_plan_id
        on record: warning, no sent receipt."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        q1 = DeliveryReceipt(
            receipt_id="rcpt-a",
            event_id="evt-4",
            delivery_plan_id="plan-a",
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        q2 = DeliveryReceipt(
            receipt_id="rcpt-b",
            event_id="evt-4",
            delivery_plan_id="plan-b",
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-2",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        await storage.append_receipt(q1)
        await storage.append_receipt(q2)

        # No delivery_plan_id on the record -> skipped, no supplemental receipt.
        record = OutboundNativeRefRecord(
            event_id="evt-4",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="77",
            delivery_plan_id=None,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )

        # Ambiguous: no supplemental sent receipt.
        sent = [r for r in storage._receipts if r.status == "sent"]
        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_multiple_queued_same_plan_different_channels_no_channel_warns(
        self,
    ) -> None:
        """Multiple queued receipts under same plan but different channels,
        no native_channel_id: warning, no sent receipt."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        plan_id = str(uuid.uuid4())
        q1 = DeliveryReceipt(
            receipt_id="rcpt-c1",
            event_id="evt-5",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="0",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        q2 = DeliveryReceipt(
            receipt_id="rcpt-c2",
            event_id="evt-5",
            delivery_plan_id=plan_id,
            target_adapter="test_mesh",
            target_channel="1",
            route_id="route-1",
            status="queued",
            created_at=datetime.now(timezone.utc),
        )
        await storage.append_receipt(q1)
        await storage.append_receipt(q2)

        # delivery_plan_id present but no native_channel_id -> ambiguous.
        record = OutboundNativeRefRecord(
            event_id="evt-5",
            adapter="test_mesh",
            native_channel_id=None,
            native_message_id="88",
            delivery_plan_id=plan_id,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )

        sent = [r for r in storage._receipts if r.status == "sent"]
        assert len(sent) == 0

    @pytest.mark.asyncio
    async def test_no_matching_queued_receipt_silent_return(self) -> None:
        """When no queued receipt matches, append_queued_to_sent_receipt
        returns silently without creating any receipt."""
        storage = _FakeStorage()
        lifecycle = DeliveryLifecycleService(logger=logging.getLogger("test"))

        record = OutboundNativeRefRecord(
            event_id="evt-no-match",
            adapter="test_mesh",
            native_channel_id="0",
            native_message_id="99",
            delivery_plan_id=None,
            metadata={},
        )

        await lifecycle.append_queued_to_sent_receipt(
            storage, record=record, now=datetime.now(timezone.utc)
        )
        assert len(storage._receipts) == 0


# ===========================================================================
# Queue backpressure / capacity rejection (characterization)
# ===========================================================================


class TestQueueBackpressure:
    """Queue full does not create false sent receipt."""

    @pytest.mark.asyncio
    async def test_full_queue_rejects_enqueue(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=2,
            max_attempts=3,
        )

        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")
        await queue.enqueue({"text": "msg2"}, 0, event_id="e2")

        with pytest.raises(MeshtasticSendError) as exc_info:
            await queue.enqueue({"text": "msg3"}, 0, event_id="e3")
        assert exc_info.value.transient is True
        assert queue.total_rejected == 1

    @pytest.mark.asyncio
    async def test_rejected_does_not_increment_sent(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=1,
        )
        await queue.enqueue({"text": "msg1"}, 0)
        with pytest.raises(MeshtasticSendError):
            await queue.enqueue({"text": "msg2"}, 0)

        assert queue.total_sent == 0
        assert queue.total_rejected == 1

    @pytest.mark.asyncio
    async def test_transient_send_failure_front_requeues(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
            max_attempts=3,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _failing_send(item: dict) -> None:
            raise MeshtasticSendError("transient", transient=True)

        result = await queue.process_one(send_fn=_failing_send)
        assert result is None
        assert queue.total_requeued == 1
        assert queue.queue_depth == 1

    @pytest.mark.asyncio
    async def test_exhausted_retries_drops_item(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
            max_attempts=1,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _failing_send(item: dict) -> None:
            raise MeshtasticSendError("transient", transient=True)

        result = await queue.process_one(send_fn=_failing_send)
        assert result is None
        assert queue.total_exhausted == 1
        assert queue.total_failed == 1
        assert queue.queue_depth == 0


# ===========================================================================
# Queue health and diagnostics (characterization)
# ===========================================================================


class TestQueueDiagnostics:
    """Queue health evidence for operators."""

    @pytest.mark.asyncio
    async def test_queue_health_snapshot(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.01,
            max_queue_size=100,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")
        await queue.enqueue({"text": "msg2"}, 0, event_id="e2")

        health = queue.queue_health
        assert health["pending_count"] == 2
        assert health["total_enqueued"] == 2
        assert health["max_queue_size"] == 100
        assert health["utilization_pct"] == 2.0

    @pytest.mark.asyncio
    async def test_queue_counters_after_successful_send(self) -> None:
        queue = MeshtasticOutboundQueue(
            delay_between_messages=0.0,
            max_queue_size=10,
        )
        await queue.enqueue({"text": "msg1"}, 0, event_id="e1")

        async def _send(item: dict) -> dict:
            return {"packet_id": 1, "channel": 0}

        result = await queue.process_one(send_fn=_send)
        assert result is not None
        assert result.delivery_result.native_message_id == "1"
        assert queue.total_sent == 1
        assert queue.total_dequeued == 1


# ===========================================================================
# Delivery state validation (characterization)
# ===========================================================================


class TestDeliveryState:
    """Delivery state vocabulary and transitions."""

    def test_receipt_transitions(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            RECEIPT_STATUSES,
            TERMINAL_RECEIPT_STATUSES,
            validate_receipt_transition,
        )

        assert "queued" in RECEIPT_STATUSES
        assert "sent" in RECEIPT_STATUSES
        assert "sent" in TERMINAL_RECEIPT_STATUSES
        assert validate_receipt_transition("queued", "sent") is True
        assert validate_receipt_transition("sent", "queued") is False

    def test_outcome_accepted_statuses(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            is_accepted_outcome_status,
        )

        assert is_accepted_outcome_status("success") is True
        assert is_accepted_outcome_status("queued") is True
        assert is_accepted_outcome_status("skipped") is False
        assert is_accepted_outcome_status("permanent_failure") is False

    def test_queued_to_sent_transition(self) -> None:
        from medre.core.engine.pipeline.delivery_state import (
            is_valid_queued_to_sent_transition,
        )

        assert is_valid_queued_to_sent_transition("queued") is True
        assert is_valid_queued_to_sent_transition("sent") is False
        assert is_valid_queued_to_sent_transition("failed") is False
