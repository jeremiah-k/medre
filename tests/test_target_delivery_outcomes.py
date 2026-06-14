"""TargetDeliveryService successful and queued delivery outcomes.

Tests for sent delivery, queued delivery, receipt status preservation,
deterministic receipt construction, adapter message ID propagation,
and persistence timestamps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.target_delivery import (
    TargetDeliveryService,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.rendering.renderer import RenderingResult

# ---------------------------------------------------------------------------
# Local fakes
# ---------------------------------------------------------------------------


class _FakeStorage:
    """In-memory storage that records appended receipts and native refs."""

    def __init__(self) -> None:
        self.receipts: list[DeliveryReceipt] = []
        self.native_refs: list[NativeMessageRef] = []

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        self.receipts.append(receipt)

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        self.native_refs.append(ref)


class _FakeRenderingPipeline:
    """Rendering pipeline stub whose render() behaviour is configurable."""

    def __init__(self, result: RenderingResult | None = None) -> None:
        self._result = result
        self._error: Exception | None = None

    def set_result(self, result: RenderingResult) -> None:
        self._result = result

    def set_error(self, exc: Exception) -> None:
        self._error = exc

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
        *,
        target_platform: str | None = None,
        max_text_chars: int | None = None,
        max_text_bytes: int | None = None,
        delivery_strategy: str | None = None,
        capability_level: str | None = None,
        source_origin_label: str | None = None,
    ) -> RenderingResult:
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        raise ValueError("No renderer registered")


class _FakeAdapter:
    """Minimal adapter with configurable deliver() and platform."""

    adapter_id: str = "test_adapter"
    platform: str = "test_platform"

    def __init__(
        self,
        result: AdapterDeliveryResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    async def deliver(self, rendering_result: Any) -> AdapterDeliveryResult | None:
        if self._error is not None:
            raise self._error
        return self._result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-001",
    event_kind: str = "message.created",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(tz=timezone.utc),
        source_adapter="src_adapter",
        source_transport_id="node-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_service(
    adapters: dict[str, Any] | None = None,
    rendering_pipeline: Any | None = None,
    storage: _FakeStorage | None = None,
) -> tuple[TargetDeliveryService, _FakeStorage]:
    """Build a TargetDeliveryService with sensible fakes."""
    _storage = storage or _FakeStorage()
    _pipeline = rendering_pipeline or _FakeRenderingPipeline(
        result=RenderingResult(
            event_id="evt-001",
            target_adapter="test_adapter",
            target_channel=None,
            payload={"text": "hello"},
        )
    )
    _diag = Diagnostician()
    _lifecycle = DeliveryLifecycleService(
        logger=logging.getLogger("test.target_delivery.lifecycle"),
    )
    svc = TargetDeliveryService(
        adapters=adapters or {},
        rendering_pipeline=_pipeline,  # type: ignore[arg-type]
        storage=_storage,  # type: ignore[arg-type]
        diagnostician=_diag,
        lifecycle=_lifecycle,
        logger=logging.getLogger("test.target_delivery"),
    )
    return svc, _storage


def _make_route_and_plan(
    adapter_id: str = "test_adapter",
    plan_id: str = "plan-001",
    method: str = "direct",
) -> tuple[Any, Any]:
    from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
    from medre.core.routing.models import Route, RouteSource, RouteTarget

    target = RouteTarget(adapter=adapter_id, channel=None)
    route = Route(
        id="route-001",
        source=RouteSource(
            adapter="src_adapter",
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-001",
        target=target,
        primary_strategy=DeliveryStrategy(method=method),
    )
    return route, plan


# ===================================================================
# Successful sent delivery
# ===================================================================


class TestSuccessfulSentDelivery:
    """Verify successful delivery with status='sent'."""

    async def test_sent_receipt_recorded(self) -> None:
        """Adapter returns result with native_message_id → sent receipt."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                native_message_id="$msg-123",
                native_channel_id="!room:server",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "sent"
        assert receipt.target_adapter == "test_adapter"
        assert receipt.error is None
        assert len(storage.receipts) == 1
        assert storage.receipts[0] is receipt

    async def test_native_ref_stored_on_sent(self) -> None:
        """Successful sent delivery persists a NativeMessageRef."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                native_message_id="$msg-456",
                native_channel_id="!room:server",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "sent"
        assert len(storage.native_refs) == 1
        nref = storage.native_refs[0]
        assert nref.native_message_id == "$msg-456"
        assert nref.adapter == "test_adapter"
        assert nref.direction == "outbound"


# ===================================================================
# Queued delivery
# ===================================================================


class TestQueuedDelivery:
    """Verify queued delivery when adapter returns delivery_status='enqueued'."""

    async def test_queued_receipt_status(self) -> None:
        """Adapter returning enqueued → receipt status='queued'."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                native_message_id=None,
                delivery_status="enqueued",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "queued"
        # No native ref for queued deliveries (no native_message_id).
        assert len(storage.native_refs) == 0

    async def test_queued_adapter_message_id_none(self) -> None:
        """Queued receipt has no adapter_message_id when native ID unavailable."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                delivery_status="enqueued",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "queued"
        assert receipt.adapter_message_id is None


# ===================================================================
# Receipt status preservation
# ===================================================================


class TestReceiptStatusPreservation:
    """Verify receipt.status matches the delivery outcome."""

    async def test_sent_status_preserved(self) -> None:
        """Successful delivery → status='sent'."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "sent"

    async def test_failed_status_preserved(self) -> None:
        """Adapter exception → status='failed'."""
        from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError

        adapter = _FakeAdapter(error=RuntimeError("fail"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert storage.receipts[0].status == "failed"

    async def test_queued_status_preserved(self) -> None:
        """Enqueued delivery → status='queued'."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(delivery_status="enqueued"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "queued"


# ===================================================================
# Deterministic receipt construction
# ===================================================================


class TestDeterministicReceiptConstruction:
    """Verify receipt lineage fields are deterministic."""

    async def test_first_attempt_has_attempt_number_one(self) -> None:
        """First delivery attempt has attempt_number=1."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.attempt_number == 1
        assert receipt.parent_receipt_id is None

    async def test_retry_attempt_has_incremented_number(self) -> None:
        """Retry attempt carries previous receipt's lineage."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        first_receipt = await svc.deliver_to_target(event, route, plan)
        assert first_receipt.attempt_number == 1

        # Simulate retry with previous_receipt.
        second_receipt = await svc.deliver_to_target(
            event, route, plan, previous_receipt=first_receipt
        )
        assert second_receipt.attempt_number == 2
        assert second_receipt.parent_receipt_id == first_receipt.receipt_id

    async def test_receipt_has_deterministic_ids(self) -> None:
        """Receipt carries receipt_id starting with 'rcpt-'."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.receipt_id.startswith("rcpt-")
        assert receipt.event_id == "evt-001"
        assert receipt.delivery_plan_id == "plan-001"

    async def test_source_and_replay_run_id_propagated(self) -> None:
        """source and replay_run_id are passed through to receipt."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(
            event, route, plan, source="replay", replay_run_id="run-42"
        )

        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-42"


# ===================================================================
# adapter_message_id propagation
# ===================================================================


class TestAdapterMessageIdPropagation:
    """Verify adapter-returned native_message_id flows to receipt."""

    async def test_native_message_id_propagated(self) -> None:
        """Adapter's native_message_id becomes receipt.adapter_message_id."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                native_message_id="$event-abc",
                native_channel_id="!room:server",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.adapter_message_id == "$event-abc"

    async def test_no_native_message_id_when_none(self) -> None:
        """When adapter returns None native_message_id, receipt field is None."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult())
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.adapter_message_id is None

    async def test_no_adapter_message_id_on_adapter_failure(self) -> None:
        """Failed delivery does not populate adapter_message_id."""
        from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError

        adapter = _FakeAdapter(error=RuntimeError("fail"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt.adapter_message_id is None


# ===================================================================
# Persistence-time timestamps
# ===================================================================


class TestPersistenceTimestamps:
    """Verify receipts use persistence-time timestamps, not stale start time."""

    async def test_receipt_created_at_is_recent(self) -> None:
        """Receipt created_at is within a small window of 'now'."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        before = datetime.now(tz=timezone.utc)
        receipt = await svc.deliver_to_target(event, route, plan)
        after = datetime.now(tz=timezone.utc)

        assert before <= receipt.created_at <= after

    async def test_next_retry_at_uses_persistence_time(self) -> None:
        """next_retry_at is computed from persistence time, not start time."""
        from medre.core.contracts.adapter import AdapterSendError
        from medre.core.engine.pipeline.target_delivery import _AdapterDeliveryError
        from medre.core.planning.delivery_plan import RetryPolicy

        adapter = _FakeAdapter(error=AdapterSendError("timeout", transient=True))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.retry_policy = RetryPolicy(max_attempts=3, backoff_base=1.0)

        before = datetime.now(tz=timezone.utc)
        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)
        after = datetime.now(tz=timezone.utc)

        receipt = storage.receipts[0]
        assert receipt.next_retry_at is not None
        # next_retry_at should be >= before (persistence time, not start time).
        assert receipt.next_retry_at >= before
        # And the receipt's created_at should also be >= before.
        assert receipt.created_at >= before
        assert receipt.created_at <= after

    async def test_native_ref_created_at_uses_persistence_time(self) -> None:
        """NativeMessageRef created_at uses persistence time."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(
                native_message_id="$msg",
                native_channel_id="!room:server",
            )
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        before = datetime.now(tz=timezone.utc)
        await svc.deliver_to_target(event, route, plan)
        after = datetime.now(tz=timezone.utc)

        nref = storage.native_refs[0]
        assert before <= nref.created_at <= after
