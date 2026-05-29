"""Focused unit tests for TargetDeliveryService and PipelineRunner delegation.

Exercises ``medre.core.engine.pipeline.target_delivery.TargetDeliveryService``
directly with small local fakes, verifying one-target execution semantics
without broad PipelineRunner lifecycle setup.  An integration test at the
bottom confirms that ``PipelineRunner.deliver_to_target`` delegates to the
service without re-owning target execution.
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
    _serialize_rendering_evidence_for_receipt,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
)
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
) -> tuple[Any, DeliveryPlan]:
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
# Rendering evidence propagation
# ===================================================================


class TestRenderingEvidencePropagation:
    """Verify rendering evidence flows from RenderingResult to receipt."""

    @staticmethod
    def _make_evidence() -> Any:
        """Create a RenderingEvidence for testing."""
        from medre.core.rendering.evidence import RenderingEvidence

        return RenderingEvidence(
            schema_version="1",
            renderer="text",
            delivery_strategy="direct",
            target_adapter="test_adapter",
            target_platform=None,
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            fallback_applied=None,
            truncated=False,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=None,
            original_text_bytes=None,
        )

    async def test_evidence_serialized_via_to_dict(self) -> None:
        """RenderingEvidence is JSON-serialized via to_dict() into receipt."""
        evidence = self._make_evidence()
        result = RenderingResult(
            event_id="evt-001",
            target_adapter="test_adapter",
            target_channel=None,
            payload={"text": "hello"},
            rendering_evidence=evidence,
        )
        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$mid"))
        pipeline = _FakeRenderingPipeline(result=result)
        svc, storage = _make_service(
            adapters={"test_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.rendering_evidence is not None
        import json

        parsed = json.loads(receipt.rendering_evidence)
        assert parsed["renderer"] == "text"
        assert parsed["schema_version"] == "1"

    async def test_no_evidence_on_failure(self) -> None:
        """Rendering failure does not attach evidence to receipt."""
        pipeline = _FakeRenderingPipeline()
        pipeline.set_error(RuntimeError("nope"))
        svc, storage = _make_service(
            adapters={"test_adapter": _FakeAdapter()},
            rendering_pipeline=pipeline,
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_RendererDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt.rendering_evidence is None


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
        adapter = _FakeAdapter(error=RuntimeError("fail"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError) as exc_info:
            await svc.deliver_to_target(event, route, plan)

        assert exc_info.value.receipt is not None
        assert exc_info.value.receipt.adapter_message_id is None


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
# Receipt status preservation
# ===================================================================


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
# Evidence serialization edge cases
# ===================================================================


class TestEvidenceSerializationEdgeCases:
    """Verify _serialize_rendering_evidence_for_receipt edge cases."""

    def test_non_callable_to_dict_returns_none(self) -> None:
        """Object with non-callable to_dict attribute returns None."""

        class _FakeEvidence:
            to_dict = "not_callable"

        result = _serialize_rendering_evidence_for_receipt(_FakeEvidence())
        assert result is None

    def test_to_dict_raises_returns_none(self) -> None:
        """Object whose to_dict() raises returns None."""

        class _BrokenEvidence:
            def to_dict(self) -> Any:
                raise RuntimeError("serialization boom")

        result = _serialize_rendering_evidence_for_receipt(_BrokenEvidence())
        assert result is None

    def test_to_dict_raises_logs_warning(self, caplog: Any) -> None:
        """Serialization failure logs a warning via the module logger."""
        import logging

        class _BrokenEvidence:
            def to_dict(self) -> Any:
                raise ValueError("bad data")

        with caplog.at_level(
            logging.WARNING,
            logger="medre.core.engine.pipeline.target_delivery",
        ):
            result = _serialize_rendering_evidence_for_receipt(_BrokenEvidence())

        assert result is None
        assert any("Failed to serialize" in msg for msg in caplog.messages)

    def test_cancelled_error_propagates(self) -> None:
        """CancelledError during serialization propagates, not swallowed."""
        import asyncio

        class _CancelEvidence:
            def to_dict(self) -> Any:
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            _serialize_rendering_evidence_for_receipt(_CancelEvidence())

    def test_str_evidence_passes_through(self) -> None:
        """String evidence is returned as-is."""
        assert _serialize_rendering_evidence_for_receipt('{"k":"v"}') == '{"k":"v"}'

    def test_dict_evidence_serialized(self) -> None:
        """Dict evidence is JSON-serialized with sort_keys=True."""
        import json

        result = _serialize_rendering_evidence_for_receipt({"b": 1, "a": 2})
        assert result is not None
        parsed = json.loads(result)
        assert list(parsed.keys()) == ["a", "b"]

    def test_callable_to_dict_succeeds(self) -> None:
        """Object with callable to_dict() is serialized correctly."""
        import json

        class _GoodEvidence:
            def to_dict(self) -> dict[str, Any]:
                return {"renderer": "text", "version": 1}

        result = _serialize_rendering_evidence_for_receipt(_GoodEvidence())
        assert result is not None
        parsed = json.loads(result)
        assert parsed["renderer"] == "text"

    def test_unsupported_type_returns_none(self) -> None:
        """Unsupported type without to_dict returns None."""
        assert _serialize_rendering_evidence_for_receipt(42) is None


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
# PipelineRunner delegation integration test
# ===================================================================


class TestPipelineRunnerDelegation:
    """Verify PipelineRunner.deliver_to_target delegates to TargetDeliveryService."""

    async def test_runner_delegates_to_target_delivery_service(
        self,
        temp_storage: Any,
    ) -> None:
        """PipelineRunner.deliver_to_target calls TargetDeliveryService.

        This integration test confirms the extraction boundary:
        PipelineRunner does not re-own target execution after the clean
        extraction.  It creates a TargetDeliveryService internally and
        delegates.
        """
        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.engine.pipeline.runner import PipelineConfig
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.routing import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router

        adapter = FakePresentationAdapter(adapter_id="dest")
        target = RouteTarget(adapter="dest")
        route = Route(
            id="delegation-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[target],
        )
        router = Router(routes=[route])
        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"dest": adapter},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="delegation-001")
        plan = DeliveryPlan(
            plan_id="delegation-route__dest__0",
            event_id=event.event_id,
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        # Persist event to match runtime contract (ingress stores before delivery).
        await temp_storage.append(event)

        try:
            receipt = await runner.deliver_to_target(event, route, plan)

            # PipelineRunner delegated to TargetDeliveryService.
            assert receipt is not None
            assert receipt.status == "sent"
            assert receipt.target_adapter == "dest"
            assert receipt.event_id == "delegation-001"

            # Adapter actually received the rendered payload.
            assert len(adapter.delivered_payloads) == 1
        finally:
            await runner.stop()

    async def test_runner_deliver_to_target_failure_delegates(
        self,
        temp_storage: Any,
    ) -> None:
        """PipelineRunner propagates _AdapterDeliveryError from service."""
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.engine.pipeline.runner import PipelineConfig
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.routing import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router

        target = RouteTarget(adapter="nowhere")
        route = Route(
            id="missing-adapter-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[target],
        )
        router = Router(routes=[route])
        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},  # No adapters registered.
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = _make_event(event_id="missing-001")
        plan = DeliveryPlan(
            plan_id="missing-adapter-route__nowhere__0",
            event_id=event.event_id,
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
        )

        # Persist event to match runtime contract (ingress stores before delivery).
        await temp_storage.append(event)

        try:
            with pytest.raises(_AdapterDeliveryError) as exc_info:
                await runner.deliver_to_target(event, route, plan)

            # Delegation preserved the failure kind.
            assert exc_info.value.failure_kind == DeliveryFailureKind.ADAPTER_MISSING
        finally:
            await runner.stop()


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
# Dead-letter on exhausted retry
# ===================================================================


class TestDeadLetterOnExhaustedRetry:
    """Verify dead-letter receipt appended when retries are exhausted."""

    async def test_dead_letter_receipt_appended(self) -> None:
        """Exhausted retry policy produces a dead_lettered receipt after failure."""
        from medre.core.planning.delivery_plan import RetryPolicy

        adapter = _FakeAdapter(error=RuntimeError("boom"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.retry_policy = RetryPolicy(max_attempts=1)

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        # Two receipts: primary failure + dead-letter.
        assert len(storage.receipts) == 2
        assert storage.receipts[0].status == "failed"
        assert storage.receipts[1].status == "dead_lettered"

    async def test_dead_letter_receipt_lineage(self) -> None:
        """Dead-letter receipt carries correct parent and attempt lineage."""
        from medre.core.planning.delivery_plan import RetryPolicy

        adapter = _FakeAdapter(error=RuntimeError("fail"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.retry_policy = RetryPolicy(max_attempts=1)

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        primary = storage.receipts[0]
        dead_letter = storage.receipts[1]
        assert dead_letter.parent_receipt_id == primary.receipt_id
        assert dead_letter.attempt_number == primary.attempt_number + 1
        assert dead_letter.target_adapter == "test_adapter"

    async def test_no_dead_letter_when_retries_remain(self) -> None:
        """Retry policy with remaining attempts does NOT produce a dead-letter."""
        from medre.core.planning.delivery_plan import RetryPolicy

        adapter = _FakeAdapter(error=RuntimeError("transient"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.retry_policy = RetryPolicy(max_attempts=3)

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        # Only the primary failure receipt — no dead-letter yet.
        assert len(storage.receipts) == 1
        assert storage.receipts[0].status == "failed"

    async def test_dead_letter_preserves_source_and_replay_run_id(self) -> None:
        """Dead-letter receipt inherits source/replay_run_id from caller."""
        from medre.core.planning.delivery_plan import RetryPolicy

        adapter = _FakeAdapter(error=RuntimeError("boom"))
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()
        plan.retry_policy = RetryPolicy(max_attempts=1)

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(
                event,
                route,
                plan,
                source="replay",
                replay_run_id="run-99",
            )

        dead_letter = storage.receipts[1]
        assert dead_letter.status == "dead_lettered"
        assert dead_letter.source == "replay"
        assert dead_letter.replay_run_id == "run-99"


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
