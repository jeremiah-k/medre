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


class _FakeRelationEnricher:
    """Passthrough relation enricher — returns the event unchanged."""

    async def enrich_for_target(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> CanonicalEvent:
        return event


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
    _enricher = _FakeRelationEnricher()
    svc = TargetDeliveryService(
        adapters=adapters or {},
        rendering_pipeline=_pipeline,  # type: ignore[arg-type]
        storage=_storage,  # type: ignore[arg-type]
        relation_enricher=_enricher,  # type: ignore[arg-type]
        diagnostician=_diag,
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
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$mid")
        )
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
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult()
        )
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
        adapter = _FakeAdapter(
            error=AdapterSendError("timeout", transient=True)
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        with pytest.raises(_AdapterDeliveryError):
            await svc.deliver_to_target(event, route, plan)

        receipt = storage.receipts[0]
        assert receipt.failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT.value

    async def test_permanent_error_classified(self) -> None:
        """AdapterPermanentError → ADAPTER_PERMANENT."""
        adapter = _FakeAdapter(
            error=AdapterPermanentError("malformed payload")
        )
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


class TestReceiptStatusPreservation:
    """Verify receipt.status matches the delivery outcome."""

    async def test_sent_status_preserved(self) -> None:
        """Successful delivery → status='sent'."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$id")
        )
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
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(delivery_status="enqueued")
        )
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
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$id")
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.attempt_number == 1
        assert receipt.parent_receipt_id is None

    async def test_retry_attempt_has_incremented_number(self) -> None:
        """Retry attempt carries previous receipt's lineage."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$id")
        )
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
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$id")
        )
        svc, storage = _make_service(adapters={"test_adapter": adapter})
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.receipt_id.startswith("rcpt-")
        assert receipt.event_id == "evt-001"
        assert receipt.delivery_plan_id == "plan-001"

    async def test_source_and_replay_run_id_propagated(self) -> None:
        """source and replay_run_id are passed through to receipt."""
        adapter = _FakeAdapter(
            result=AdapterDeliveryResult(native_message_id="$id")
        )
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

        try:
            with pytest.raises(_AdapterDeliveryError) as exc_info:
                await runner.deliver_to_target(event, route, plan)

            # Delegation preserved the failure kind.
            assert exc_info.value.failure_kind == DeliveryFailureKind.ADAPTER_MISSING
        finally:
            await runner.stop()

