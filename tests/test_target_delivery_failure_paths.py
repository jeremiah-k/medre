"""TargetDeliveryService failure and error paths.

Tests for rendering failures, adapter exceptions, missing adapters,
failure kind classification, deadline exceeded, invalid delivery strategy,
adapters without deliver(), CancelledError propagation, and failure kind
propagation on re-raised errors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.target_delivery import (
    TargetDeliveryService,
    _AdapterDeliveryError,
    _RendererDeliveryError,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import DeliveryFailureKind
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
# Rendering failure
# ===================================================================


class TestRenderingFailure:
    """Verify rendering failure produces correct error and receipt."""

    async def test_rendering_error_raises_renderer_delivery_error(self) -> None:
        """Rendering failure raises _RendererDeliveryError."""
        pipeline = _FakeRenderingPipeline()
        pipeline.set_error(RuntimeError("no renderer for this event"))
        svc, storage = _make_service(
            adapters={"test_adapter": _FakeAdapter()},
            rendering_pipeline=pipeline,
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_RendererDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.adapter_id == "test_adapter"
        assert "Rendering failed" in err.error
        assert err.receipt is not None
        assert err.receipt.status == "failed"

    async def test_rendering_failure_receipt_failure_kind(self) -> None:
        """Rendering failure receipt has failure_kind=RENDERER_FAILURE."""
        pipeline = _FakeRenderingPipeline()
        pipeline.set_error(ValueError("unsupported event kind"))
        svc, storage = _make_service(
            adapters={"test_adapter": _FakeAdapter()},
            rendering_pipeline=pipeline,
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_RendererDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        receipt = exc_info.value.receipt
        assert receipt is not None
        assert receipt.failure_kind == DeliveryFailureKind.RENDERER_FAILURE.value

    async def test_rendering_failure_diagnostician_notified(self) -> None:
        """Rendering failure records in diagnostician."""
        pipeline = _FakeRenderingPipeline()
        pipeline.set_error(RuntimeError("render boom"))
        diag = Diagnostician()
        svc, storage = _make_service(
            adapters={"test_adapter": _FakeAdapter()},
            rendering_pipeline=pipeline,
        )
        # Override diagnostician to the one we can inspect.
        svc._diagnostician = diag
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_RendererDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        snap = diag.snapshot()
        assert snap["renderer_failures"]["test_adapter"] == 1


# ===================================================================
# Adapter lookup failure
# ===================================================================


class TestAdapterLookupFailure:
    """Verify adapter-missing path produces correct error and receipt."""

    async def test_missing_adapter_raises_adapter_delivery_error(self) -> None:
        """Missing adapter raises _AdapterDeliveryError with ADAPTER_MISSING."""
        svc, storage = _make_service(adapters={})
        event = _make_event()
        route, plan = _make_route_and_plan(adapter_id="nonexistent")

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.adapter_id == "nonexistent"
        assert err.failure_kind == DeliveryFailureKind.ADAPTER_MISSING

    async def test_missing_adapter_receipt_persisted(self) -> None:
        """Missing adapter still persists a failure receipt."""
        svc, storage = _make_service(adapters={})
        event = _make_event()
        route, plan = _make_route_and_plan(adapter_id="ghost")

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.receipts) == 1
        receipt = storage.receipts[0]
        assert receipt.status == "failed"
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_MISSING.value
        assert receipt.target_adapter == "ghost"

    async def test_missing_adapter_receipt_on_error_object(self) -> None:
        """The _AdapterDeliveryError carries the persisted receipt."""
        svc, storage = _make_service(adapters={})
        event = _make_event()
        route, plan = _make_route_and_plan(adapter_id="absent")

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err_receipt = exc_info.value.receipt
        assert err_receipt is not None
        assert err_receipt.failure_kind == DeliveryFailureKind.ADAPTER_MISSING.value


# ===================================================================
# Adapter delivery exception
# ===================================================================


class TestAdapterDeliveryException:
    """Verify adapter.deliver() exceptions produce correct receipts."""

    async def test_adapter_raises_runtime_error(self) -> None:
        """Adapter RuntimeError → _AdapterDeliveryError with failed receipt."""
        adapter = _FakeAdapter(error=RuntimeError("transport down"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert "RuntimeError" in err.error
        assert err.receipt is not None
        assert err.receipt.status == "failed"

    async def test_adapter_failure_receipt_persisted_before_raise(self) -> None:
        """Failure receipt is appended to storage before re-raising."""
        adapter = _FakeAdapter(error=RuntimeError("boom"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.receipts) == 1
        assert storage.receipts[0].status == "failed"

    async def test_no_native_ref_on_adapter_failure(self) -> None:
        """Failed deliveries do not store native refs."""
        adapter = _FakeAdapter(error=RuntimeError("fail"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.native_refs) == 0


# ===================================================================
# failure_kind classification
# ===================================================================


class TestFailureKindClassification:
    """Verify RetryExecutor.classify_failure integration with receipts."""

    async def test_transient_error_classified(self) -> None:
        """AdapterSendError(transient=True) → ADAPTER_TRANSIENT."""
        adapter = _FakeAdapter(error=AdapterSendError("timeout", transient=True))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        receipt = storage.receipts[0]
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT.value

    async def test_permanent_error_classified(self) -> None:
        """AdapterPermanentError → ADAPTER_PERMANENT."""
        adapter = _FakeAdapter(error=AdapterPermanentError("malformed payload"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        receipt = storage.receipts[0]
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT.value

    async def test_generic_runtime_error_classified_permanent(self) -> None:
        """Generic RuntimeError → ADAPTER_PERMANENT (not transient)."""
        adapter = _FakeAdapter(error=RuntimeError("unknown"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        receipt = storage.receipts[0]
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT.value

    async def test_connection_error_classified_transient(self) -> None:
        """ConnectionError → ADAPTER_TRANSIENT."""
        adapter = _FakeAdapter(error=ConnectionError("refused"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        receipt = storage.receipts[0]
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT.value


# ===================================================================
# failure_kind propagation on re-raised _AdapterDeliveryError
# ===================================================================


class TestFailureKindPropagationOnReRaise:
    """Verify _AdapterDeliveryError.failure_kind matches the receipt."""

    async def test_transient_error_failure_kind_propagated(self) -> None:
        """Exception failure_kind matches receipt for transient adapter error."""
        adapter = _FakeAdapter(error=AdapterSendError("timeout", transient=True))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        receipt = storage.receipts[0]
        assert err.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT.value
        # The exception's failure_kind must agree with the receipt.
        assert err.failure_kind.value == receipt.failure_kind

    async def test_permanent_error_failure_kind_propagated(self) -> None:
        """Exception failure_kind matches receipt for permanent adapter error."""
        adapter = _FakeAdapter(error=AdapterPermanentError("malformed payload"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        receipt = storage.receipts[0]
        assert err.failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT
        assert err.failure_kind.value == receipt.failure_kind

    async def test_connection_error_failure_kind_propagated(self) -> None:
        """Exception failure_kind matches receipt for connection error."""
        adapter = _FakeAdapter(error=ConnectionError("refused"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        receipt = storage.receipts[0]
        assert err.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT
        assert err.failure_kind.value == receipt.failure_kind


# ===================================================================
# Deadline exceeded
# ===================================================================


class TestDeadlineExceeded:
    """Verify deadline-exceeded path produces correct error and receipt."""

    async def test_deadline_exceeded_raises_adapter_delivery_error(self) -> None:
        """Plan with past deadline raises _AdapterDeliveryError."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.deadline = datetime(2020, 1, 1, tzinfo=timezone.utc)

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.failure_kind == DeliveryFailureKind.DEADLINE_EXCEEDED
        assert "deadline exceeded" in err.error.lower()

    async def test_deadline_exceeded_receipt_persisted(self) -> None:
        """Deadline-exceeded path persists a failure receipt."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.deadline = datetime(2020, 1, 1, tzinfo=timezone.utc)

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.receipts) == 1
        receipt = storage.receipts[0]
        assert receipt.status == "failed"
        assert receipt.failure_kind == DeliveryFailureKind.DEADLINE_EXCEEDED.value

    async def test_no_deadline_allows_delivery(self) -> None:
        """Plan without deadline does not block a successful delivery."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        assert plan.deadline is None

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "sent"


# ===================================================================
# Invalid delivery strategy
# ===================================================================


class TestInvalidDeliveryStrategy:
    """Verify invalid strategy method produces PLANNER_FAILURE."""

    async def test_invalid_strategy_raises_renderer_delivery_error(self) -> None:
        """Unknown strategy method raises _RendererDeliveryError."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan(method="bogus_strategy")

        with pytest.raises(_RendererDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.adapter_id == "test_adapter"
        assert "delivery strategy method" in err.error.lower()

    async def test_invalid_strategy_receipt_has_planner_failure(self) -> None:
        """Invalid strategy receipt has failure_kind=PLANNER_FAILURE."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan(method="nonexistent")

        with pytest.raises(_RendererDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        receipt = exc_info.value.receipt
        assert receipt is not None
        assert receipt.failure_kind == DeliveryFailureKind.PLANNER_FAILURE.value

    async def test_invalid_strategy_receipt_persisted(self) -> None:
        """Invalid strategy persists a failure receipt."""
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan(method="garbage")

        with pytest.raises(_RendererDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.receipts) == 1
        assert storage.receipts[0].status == "failed"


# ===================================================================
# Adapter without deliver() method
# ===================================================================


class TestAdapterWithoutDeliver:
    """Verify adapter lacking deliver() produces ADAPTER_PERMANENT error."""

    @staticmethod
    def _make_adapter_without_deliver() -> Any:
        """Create an adapter-like object without a deliver() method."""

        class _NoDeliverAdapter:
            adapter_id: str = "test_adapter"
            platform: str = "test_platform"

        return _NoDeliverAdapter()

    async def test_no_deliver_raises_adapter_delivery_error(self) -> None:
        """Adapter without deliver() raises _AdapterDeliveryError."""
        adapter = self._make_adapter_without_deliver()
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.adapter_id == "test_adapter"
        assert "no deliver() method" in err.error.lower()

    async def test_no_deliver_receipt_has_adapter_permanent(self) -> None:
        """Adapter without deliver() receipt has failure_kind=ADAPTER_PERMANENT."""
        adapter = self._make_adapter_without_deliver()
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        receipt = exc_info.value.receipt
        assert receipt is not None
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_PERMANENT.value

    async def test_no_deliver_receipt_persisted(self) -> None:
        """Adapter without deliver() still persists a failure receipt."""
        adapter = self._make_adapter_without_deliver()
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        assert len(storage.receipts) == 1
        assert storage.receipts[0].status == "failed"


# ===================================================================
# CancelledError propagation
# ===================================================================


class TestCancelledErrorPropagation:
    """Verify CancelledError propagates without being classified as failure."""

    @staticmethod
    def _make_cancel_adapter() -> Any:
        """Create an adapter that raises CancelledError from deliver()."""
        import asyncio

        class _CancelAdapter:
            adapter_id: str = "test_adapter"
            platform: str = "test_platform"

            async def deliver(
                self, rendering_result: Any
            ) -> AdapterDeliveryResult | None:
                raise asyncio.CancelledError()

        return _CancelAdapter()

    async def test_cancelled_error_propagates_directly(self) -> None:
        """asyncio.CancelledError is re-raised, not caught as adapter failure."""
        import asyncio

        adapter = self._make_cancel_adapter()
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(asyncio.CancelledError):
            await svc.deliver_to_target(event, route, plan)

    async def test_cancelled_error_no_receipt_persisted(self) -> None:
        """CancelledError does not persist a failure receipt."""
        import asyncio

        adapter = self._make_cancel_adapter()
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(asyncio.CancelledError):
            await svc.deliver_to_target(event, route, plan)

        # No receipts — CancelledError bypasses receipt recording.
        assert len(storage.receipts) == 0
