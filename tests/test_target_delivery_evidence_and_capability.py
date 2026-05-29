"""TargetDeliveryService evidence, dead-letter, capability, and integration tests.

Tests for rendering evidence propagation, evidence serialization edge cases,
dead-letter on exhausted retry, capability level propagation, invalid
capability decision handling, and PipelineRunner delegation.
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
    _AdapterDeliveryError,
    _RendererDeliveryError,
    _serialize_rendering_evidence_for_receipt,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
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


class _CapabilityRecordingPipeline:
    """Rendering pipeline stub that records capability_level from render calls."""

    def __init__(self, result: RenderingResult | None = None) -> None:
        self._result = result
        self.recorded_capability_level: str | None = None
        self.recorded_delivery_strategy: str | None = None

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
    ) -> RenderingResult:
        self.recorded_capability_level = capability_level
        self.recorded_delivery_strategy = delivery_strategy
        if self._result is not None:
            return self._result
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload={"text": "rendered"},
        )


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
# Capability-level rendering pipeline integration
# ===================================================================


class TestCapabilityLevelPropagation:
    """Verify that capability_level and delivery_strategy flow to the
    rendering pipeline via TargetDeliveryService."""

    @staticmethod
    def _make_caps_adapter(
        replies: str = "native",
        text: bool = True,
    ) -> tuple[Any, Any]:
        """Create a fake adapter with configurable capabilities."""
        from medre.core.contracts.adapter import AdapterCapabilities

        caps = AdapterCapabilities(replies=replies, text=text)

        class _CapAdapter:
            adapter_id: str = "cap_adapter"
            platform: str = "test_platform"
            _capabilities: AdapterCapabilities = caps

            async def deliver(self, rendering_result: Any) -> Any:
                return AdapterDeliveryResult(native_message_id="$cap-msg")

        return _CapAdapter(), caps

    async def test_text_true_native_capability_level(self) -> None:
        """message.text with text=True -> capability_level='native'."""
        adapter, caps = self._make_caps_adapter(text=True)
        pipeline = _CapabilityRecordingPipeline()
        svc, storage = _make_service(
            adapters={"cap_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        event = _make_event(event_kind="message.text")
        route, plan = _make_route_and_plan(adapter_id="cap_adapter", method="direct")

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "native"

    async def test_skip_strategy_does_not_render(self) -> None:
        """Plan with method='skip' does not invoke rendering pipeline."""
        adapter, caps = self._make_caps_adapter()
        pipeline = _CapabilityRecordingPipeline()
        svc, storage = _make_service(
            adapters={"cap_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        event = _make_event(event_kind="message.text")
        route, plan = _make_route_and_plan(adapter_id="cap_adapter", method="skip")

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "suppressed"
        assert pipeline.recorded_capability_level is None

    async def test_reply_fallback_capability_level(self) -> None:
        """Reply relation + replies='fallback' -> capability_level='fallback',
        delivery_strategy='fallback_text'."""
        adapter, _caps = self._make_caps_adapter(replies="fallback", text=True)
        pipeline = _CapabilityRecordingPipeline()
        svc, storage = _make_service(
            adapters={"cap_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        reply_relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="cap_adapter",
                native_channel_id="ch-0",
                native_message_id="native-001",
            ),
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-reply-fallback",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(tz=timezone.utc),
            source_adapter="src_adapter",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(reply_relation,),
            payload={"text": "a reply"},
            metadata=EventMetadata(),
        )
        route, plan = _make_route_and_plan(
            adapter_id="cap_adapter", method="fallback_text"
        )

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "fallback"
        assert pipeline.recorded_delivery_strategy == "fallback_text"

    async def test_reply_native_capability_level(self) -> None:
        """Reply relation + replies='native' -> capability_level='native',
        delivery_strategy='direct'."""
        adapter, _caps = self._make_caps_adapter(replies="native", text=True)
        pipeline = _CapabilityRecordingPipeline()
        svc, storage = _make_service(
            adapters={"cap_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        reply_relation = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="cap_adapter",
                native_channel_id="ch-0",
                native_message_id="native-001",
            ),
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-reply-native",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(tz=timezone.utc),
            source_adapter="src_adapter",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(reply_relation,),
            payload={"text": "a reply"},
            metadata=EventMetadata(),
        )
        route, plan = _make_route_and_plan(adapter_id="cap_adapter", method="direct")

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "native"
        assert pipeline.recorded_delivery_strategy == "direct"


# ===================================================================
# Invalid capability decision from resolver
# ===================================================================


class TestInvalidCapabilityDecision:
    """Verify invalid resolver capability_level is treated as planner failure."""

    async def test_invalid_capability_level_planner_failure(self) -> None:
        """Invalid capability_level from resolver produces PLANNER_FAILURE."""
        from unittest.mock import patch

        from medre.core.planning.capability_decision import CapabilityDecision

        adapter = _FakeAdapter(result=AdapterDeliveryResult(native_message_id="$id"))
        pipeline = _FakeRenderingPipeline(
            result=RenderingResult(
                event_id="evt-001",
                target_adapter="test_adapter",
                target_channel=None,
                payload={"text": "hello"},
            )
        )
        diag = Diagnostician()
        svc, storage = _make_service(
            adapters={"test_adapter": adapter},
            rendering_pipeline=pipeline,
        )
        svc._diagnostician = diag
        event = _make_event()
        route, plan = _make_route_and_plan()

        # Monkeypatch resolver to return invalid capability_level.
        bad_decision = CapabilityDecision(
            target_adapter="test_adapter",
            event_kind="message.created",
            capability_level="bogus",  # type: ignore[arg-type]
            delivery_strategy="direct",
            supported=True,
            capability_field=None,
            reason=None,
        )
        with patch(
            "medre.core.engine.pipeline.target_delivery._resolver.decide",
            return_value=bad_decision,
        ):
            with pytest.raises(_RendererDeliveryError) as exc_info:
                await svc.deliver_to_target(event, route, plan)

        err = exc_info.value
        assert err.failure_kind == DeliveryFailureKind.PLANNER_FAILURE
        assert err.receipt is not None
        assert err.receipt.failure_kind == DeliveryFailureKind.PLANNER_FAILURE.value
        assert err.receipt.status == "failed"
        # Receipt was persisted.
        assert len(storage.receipts) == 1
        assert storage.receipts[0] is err.receipt
        # Diagnostician was notified.
        snap = diag.snapshot()
        assert "planner_failures" in snap
        assert "evt-001" in snap["planner_failures"]
        assert snap["planner_failures"]["evt-001"] >= 1


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
