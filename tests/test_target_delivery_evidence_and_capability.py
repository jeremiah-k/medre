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
        plan.capability_level = "fallback"

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
    """Verify invalid plan capability_level is treated as planner failure."""

    async def test_invalid_capability_level_planner_failure(self) -> None:
        """Invalid capability_level from delivery plan produces PLANNER_FAILURE."""
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
        # Set an invalid capability_level on the plan directly.
        plan.capability_level = "bogus"  # type: ignore[assignment]

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


# ===========================================================================
# _normalize_mapping: recursive metadata normalization
# ===========================================================================


class TestNormalizeMapping:
    """Focused tests for ``_normalize_mapping`` in ``target_delivery.py``.

    The function recursively converts ``MappingProxyType`` and other
    ``Mapping`` subclasses to plain ``dict`` so that ``msgspec.json.encode``
    never encounters a ``mappingproxy``.  It creates a copy — never mutates.
    """

    @staticmethod
    def _normalize(value: object) -> object:
        """Import and call the private function under test."""
        from medre.core.engine.pipeline.target_delivery import _normalize_mapping

        return _normalize_mapping(value)

    def test_plain_dict_values_recursed(self) -> None:
        """Plain dict is recursed into (values normalised) but top level stays dict."""
        result = self._normalize({"a": 1})
        assert result == {"a": 1}
        assert isinstance(result, dict)

    def test_mapping_proxy_converted_to_dict(self) -> None:
        """MappingProxyType at top level is converted to plain dict."""
        from types import MappingProxyType

        proxy = MappingProxyType({"key": "val"})
        result = self._normalize(proxy)
        assert isinstance(result, dict)
        assert result == {"key": "val"}
        assert not isinstance(result, MappingProxyType)

    def test_nested_mapping_proxy_unwrapped(self) -> None:
        """MappingProxyType nested inside a dict is unwrapped."""
        from types import MappingProxyType

        data = {"outer": MappingProxyType({"inner": 42})}
        result = self._normalize(data)
        assert isinstance(result, dict)
        outer = result["outer"]
        assert isinstance(outer, dict)
        assert outer == {"inner": 42}

    def test_list_items_recursed(self) -> None:
        """List elements are recursed into."""
        from types import MappingProxyType

        data = [MappingProxyType({"x": 1}), "scalar", 3]
        result = self._normalize(data)
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        assert result[0] == {"x": 1}
        assert result[1] == "scalar"
        assert result[2] == 3

    def test_tuple_items_recursed_as_list(self) -> None:
        """Tuple elements are recursed into and returned as list."""
        from types import MappingProxyType

        data = (MappingProxyType({"y": 2}),)
        result = self._normalize(data)
        assert isinstance(result, list)
        assert result[0] == {"y": 2}

    def test_deeply_nested_structure_fully_normalised(self) -> None:
        """Three levels of MappingProxyType nesting are fully unwrapped."""
        from types import MappingProxyType

        deep = MappingProxyType({"leaf": True})
        mid = MappingProxyType({"deep": deep})
        top = {"mid": mid, "list": [MappingProxyType({"item": 1})]}
        result = self._normalize(top)
        assert isinstance(result, dict)
        mid_val = result["mid"]
        assert isinstance(mid_val, dict)
        deep_val = mid_val["deep"]
        assert isinstance(deep_val, dict)
        assert deep_val == {"leaf": True}
        list_val = result["list"]
        assert isinstance(list_val, list)
        assert isinstance(list_val[0], dict)
        assert list_val[0] == {"item": 1}

    def test_scalars_pass_through(self) -> None:
        """Non-collection values pass through unchanged."""
        for val in (42, 3.14, True, None, "hello", b"bytes"):
            assert self._normalize(val) is val

    def test_user_dict_mapping_subclass_converted(self) -> None:
        """UserDict (Mapping subclass) is converted to plain dict."""
        from collections import UserDict

        ud = UserDict({"k": "v"})
        result = self._normalize(ud)
        assert isinstance(result, dict)
        assert result == {"k": "v"}
        assert not isinstance(result, UserDict)

    def test_does_not_mutate_input(self) -> None:
        """Original MappingProxyType is never mutated (it's immutable anyway)."""
        from types import MappingProxyType

        proxy = MappingProxyType({"a": MappingProxyType({"b": 1})})
        result = self._normalize(proxy)
        assert isinstance(result, dict)
        # Original proxy still wraps immutable data
        assert isinstance(proxy["a"], MappingProxyType)


# ===========================================================================
# Native-ref authority: rendering boundary and outbound storage
# ===========================================================================


class TestRenderingBoundaryNoPayloadMutation:
    """Pipeline does not modify RenderingResult.payload after rendering.

    After ``render()`` returns, the pipeline stamps ``delivery_plan_id`` but
    must NOT touch the payload — relation construction is the renderer's
    job, not the pipeline's.
    """

    async def test_payload_not_mutated_after_render(self) -> None:
        """RenderingResult.payload is delivered to adapter exactly as rendered."""
        original_payload = {
            "text": "hello",
            "reply_to": "$native-msg-001",
        }

        class _PayloadRecordingAdapter:
            adapter_id: str = "rec_adapter"
            platform: str = "test"

            def __init__(self) -> None:
                self.delivered_payloads: list[Any] = []

            async def deliver(self, rendering_result: Any) -> Any:
                self.delivered_payloads.append(dict(rendering_result.payload))
                return AdapterDeliveryResult(
                    native_message_id="$delivered-001",
                    native_channel_id="ch-0",
                )

        adapter = _PayloadRecordingAdapter()

        class _FixedPayloadPipeline:
            async def render(
                self,
                event: Any,
                target_adapter: str,
                target_channel: str | None = None,
                **kwargs: Any,
            ) -> Any:
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=target_adapter,
                    target_channel=target_channel,
                    payload=dict(original_payload),
                )

        svc, _ = _make_service(
            adapters={"rec_adapter": adapter},
            rendering_pipeline=_FixedPayloadPipeline(),
        )
        event = _make_event()
        route, plan = _make_route_and_plan(adapter_id="rec_adapter")

        await svc.deliver_to_target(event, route, plan)

        assert len(adapter.delivered_payloads) == 1
        assert adapter.delivered_payloads[0] == original_payload


class TestRendererProtocolNoStorageAccess:
    """Renderer and RenderingContext have no storage dependency.

    This architectural boundary test proves that the rendering layer
    is completely decoupled from storage — renderers receive context
    and events, never storage backends.
    """

    def test_rendering_context_has_no_storage_attribute(self) -> None:
        """RenderingContext dataclass has no storage-related fields."""
        from medre.core.rendering.renderer import RenderingContext

        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter="test",
        )
        assert not hasattr(ctx, "storage")
        assert not hasattr(ctx, "_storage")
        assert not hasattr(ctx, "backend")

    def test_renderer_protocol_no_storage_parameter(self) -> None:
        """Renderer.render() signature has no storage parameter."""
        import inspect

        from medre.core.rendering.renderer import Renderer

        sig = inspect.signature(Renderer.render)
        params = list(sig.parameters.keys())
        assert "storage" not in params
        assert "backend" not in params

    def test_rendering_pipeline_no_storage_init(self) -> None:
        """RenderingPipeline.__init__ does not accept storage."""
        import inspect

        from medre.core.rendering.renderer import RenderingPipeline

        sig = inspect.signature(RenderingPipeline.__init__)
        params = list(sig.parameters.keys())
        assert "storage" not in params
        assert "backend" not in params


class TestOutboundNativeRefNullChannel:
    """Outbound native ref with NULL channel is stored and resolvable.

    LXMF adapters return ``native_channel_id=None`` — the pipeline must
    store this correctly and the ref must be resolvable for future
    cross-transport relation lookups.
    """

    async def test_null_channel_native_ref_stored_on_delivery(self) -> None:
        """Adapter returning native_channel_id=None produces a stored
        NativeMessageRef with NULL channel."""

        adapter_result = AdapterDeliveryResult(
            native_message_id="lxmf-hash-001",
            native_channel_id=None,
        )
        adapter = _FakeAdapter(result=adapter_result)

        class _MinimalPipeline:
            async def render(
                self,
                event: Any,
                target_adapter: str,
                target_channel: str | None = None,
                **kwargs: Any,
            ) -> Any:
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=target_adapter,
                    target_channel=target_channel,
                    payload={"text": "hello"},
                )

        svc, storage = _make_service(
            adapters={"test_adapter": adapter},
            rendering_pipeline=_MinimalPipeline(),
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        receipt = await svc.deliver_to_target(event, route, plan)

        assert receipt.status == "sent"
        assert len(storage.native_refs) == 1
        nref = storage.native_refs[0]
        assert nref.native_message_id == "lxmf-hash-001"
        assert nref.native_channel_id is None
        assert nref.direction == "outbound"
        assert nref.event_id == "evt-001"

    async def test_outbound_metadata_with_mapping_proxy_stored(self) -> None:
        """Adapter returning MappingProxyType metadata has it normalised
        before storage as native ref metadata."""
        from types import MappingProxyType

        adapter_result = AdapterDeliveryResult(
            native_message_id="msg-proxy",
            native_channel_id="0",
            metadata=MappingProxyType(
                {
                    "transport": MappingProxyType({"ack": True}),
                }
            ),
        )
        adapter = _FakeAdapter(result=adapter_result)

        class _MinimalPipeline:
            async def render(
                self,
                event: Any,
                target_adapter: str,
                target_channel: str | None = None,
                **kwargs: Any,
            ) -> Any:
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter=target_adapter,
                    target_channel=target_channel,
                    payload={"text": "hello"},
                )

        svc, storage = _make_service(
            adapters={"test_adapter": adapter},
            rendering_pipeline=_MinimalPipeline(),
        )
        event = _make_event()
        route, plan = _make_route_and_plan()

        await svc.deliver_to_target(event, route, plan)

        assert len(storage.native_refs) == 1
        nref = storage.native_refs[0]
        # Metadata is JSON-safe (no MappingProxyType).
        import json

        serialised = json.dumps(nref.metadata)
        assert "transport" in serialised
        parsed = json.loads(serialised)
        assert parsed["transport"]["ack"] is True
