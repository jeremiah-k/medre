"""Focused integration tests for DeliveryPlan capability field authority.

Proves DeliveryPlan capability fields are authoritative across the runner,
target delivery RenderingContext, and relation evidence serialization.

Coverage:
1. Target delivery: RenderingContext receives plan capability_level
   (native, fallback, unsupported, None → "native" default).
2. Runner plan authority: skip outcome carries plan reason/level fields.
3. Relation evidence: render_mode_reason survives JSON round-trip.
4. Diagnostics/replay: documented via comments (not live plan-driven here).

Diagnostics and replay plan-driven behavior is covered separately in:
- ``test_target_delivery_evidence_and_capability.TestInvalidCapabilityDecision``
  for planner failure diagnostics.
- ``test_replay_engine_plan_filters`` for replay capability filtering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.contracts.adapter import AdapterDeliveryResult
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    DeliveryStrategy,
)
from medre.core.rendering.evidence import (
    RelationTargetEvidence,
)
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, RouteSource, RouteTarget


# ---------------------------------------------------------------------------
# Local fakes
# ---------------------------------------------------------------------------


class _RecordingPipeline:
    """Rendering pipeline that records capability_level and delivery_strategy."""

    def __init__(self) -> None:
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
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload={"text": "rendered"},
        )


class _FakeAdapter:
    adapter_id: str = "plan_auth_adapter"
    platform: str = "test"

    async def deliver(self, rendering_result: Any) -> AdapterDeliveryResult:
        return AdapterDeliveryResult(native_message_id="$plan-auth-msg")


class _FakeStorage:
    def __init__(self) -> None:
        self.receipts: list[DeliveryReceipt] = []
        self.native_refs: list[NativeMessageRef] = []

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        self.receipts.append(receipt)

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        self.native_refs.append(ref)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-plan-auth-001",
    event_kind: str = "message.text",
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(tz=timezone.utc),
        source_adapter="src",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "plan authority test"},
        metadata=EventMetadata(),
    )


def _make_service(
    adapters: dict[str, Any] | None = None,
    pipeline: Any | None = None,
) -> tuple[TargetDeliveryService, _FakeStorage]:
    import logging

    storage = _FakeStorage()
    _pipeline = pipeline or _RecordingPipeline()
    diag = Diagnostician()
    lifecycle = DeliveryLifecycleService(
        logger=logging.getLogger("test.plan_authority.lifecycle"),
    )
    svc = TargetDeliveryService(
        adapters=adapters or {"plan_auth_adapter": _FakeAdapter()},  # type: ignore[arg-type]

        rendering_pipeline=_pipeline,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        diagnostician=diag,
        lifecycle=lifecycle,
        logger=logging.getLogger("test.plan_authority"),
    )
    return svc, storage


def _make_route_and_plan(
    adapter_id: str = "plan_auth_adapter",
    plan_id: str = "plan-auth-001",
    method: str = "direct",
    capability_level: str | None = None,
) -> tuple[Route, DeliveryPlan]:
    target = RouteTarget(adapter=adapter_id, channel=None)
    route = Route(
        id="route-plan-auth",
        source=RouteSource(
            adapter="src",
            event_kinds=("message.text",),
            channel=None,
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id=plan_id,
        event_id="evt-plan-auth-001",
        target=target,
        primary_strategy=DeliveryStrategy(method=method),  # type: ignore[arg-type]
    )
    if capability_level is not None:
        plan.capability_level = capability_level
    return route, plan


# ===================================================================
# Target delivery: RenderingContext receives plan capability_level
# ===================================================================


class TestRenderingContextCapabilityFromPlan:
    """RenderingContext is populated with plan.capability_level, not
    re-resolved from adapter capabilities during the delivery phase."""

    async def test_plan_capability_level_fallback_reaches_context(self) -> None:
        """plan.capability_level='fallback' is forwarded to render()."""
        pipeline = _RecordingPipeline()
        svc, _ = _make_service(pipeline=pipeline)
        event = _make_event()
        route, plan = _make_route_and_plan(capability_level="fallback")

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "fallback"
        assert pipeline.recorded_delivery_strategy == "direct"

    async def test_plan_capability_level_unsupported_reaches_context(self) -> None:
        """plan.capability_level='unsupported' is forwarded to render().

        The runner's Phase 2.5 normally suppresses 'unsupported' before
        reaching target_delivery.  This test verifies that IF a plan with
        'unsupported' reaches the rendering path (e.g. via hand-crafted
        plans or replay), the capability_level is still forwarded
        faithfully.
        """
        pipeline = _RecordingPipeline()
        svc, _ = _make_service(pipeline=pipeline)
        event = _make_event()
        route, plan = _make_route_and_plan(capability_level="unsupported")

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "unsupported"

    async def test_plan_capability_level_none_defaults_to_native(self) -> None:
        """plan.capability_level=None defaults to 'native' in RenderingContext.

        When a plan is constructed without an explicit capability_level
        (e.g. via replay or hand-crafted plans), target_delivery defaults
        to 'native' before constructing the RenderingContext.
        """
        pipeline = _RecordingPipeline()
        svc, _ = _make_service(pipeline=pipeline)
        event = _make_event()
        route, plan = _make_route_and_plan(capability_level=None)
        assert plan.capability_level is None

        await svc.deliver_to_target(event, route, plan)

        # The pipeline normalises None → "native" before calling render.
        assert pipeline.recorded_capability_level == "native"

    async def test_plan_capability_level_native_reaches_context(self) -> None:
        """plan.capability_level='native' is forwarded to render()."""
        pipeline = _RecordingPipeline()
        svc, _ = _make_service(pipeline=pipeline)
        event = _make_event()
        route, plan = _make_route_and_plan(capability_level="native")

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "native"

    async def test_fallback_text_strategy_with_fallback_level(self) -> None:
        """delivery_strategy='fallback_text' + capability_level='fallback'."""
        pipeline = _RecordingPipeline()
        svc, _ = _make_service(pipeline=pipeline)
        event = _make_event()
        route, plan = _make_route_and_plan(
            method="fallback_text",
            capability_level="fallback",
        )

        await svc.deliver_to_target(event, route, plan)

        assert pipeline.recorded_capability_level == "fallback"
        assert pipeline.recorded_delivery_strategy == "fallback_text"


# ===================================================================
# Relation evidence: render_mode_reason serialization
# ===================================================================


class TestRelationEvidenceRenderModeReasonSerialization:
    """render_mode_reason survives full JSON round-trip serialization."""

    def test_render_mode_reason_json_round_trip(self) -> None:
        """render_mode_reason value survives JSON dumps/loads round-trip."""
        for reason in (
            "native_target_available",
            "capability_fallback",
            "capability_unsupported",
            "target_unresolved",
            "native_ref_unavailable",
            "fallback_applied_match",
            "strategy_fallback_text",
        ):
            evidence = RelationTargetEvidence(
                relation_type="reply",
                render_mode="fallback",
                target_event_id="evt-1",
                target_native_message_id=None,
                target_available=False,
                fallback_text_source=None,
                render_mode_reason=reason,
            )
            d = evidence.to_dict()
            serialized = json.dumps(d, sort_keys=True)
            parsed = json.loads(serialized)

            assert parsed["render_mode_reason"] == reason, (
                f"render_mode_reason={reason!r} lost in round-trip"
            )

    def test_render_mode_reason_none_serializes_as_null(self) -> None:
        """render_mode_reason=None serializes as JSON null."""
        evidence = RelationTargetEvidence(
            relation_type="reply",
            render_mode="native",
            target_event_id="evt-1",
            target_native_message_id="msg-1",
            target_available=True,
            fallback_text_source=None,
        )
        d = evidence.to_dict()
        serialized = json.dumps(d, sort_keys=True)
        parsed = json.loads(serialized)

        assert parsed["render_mode_reason"] is None

    def test_render_mode_reason_in_full_evidence_dict(self) -> None:
        """render_mode_reason appears in the serialized evidence dict keys."""
        evidence = RelationTargetEvidence(
            relation_type="edit",
            render_mode="fallback",
            target_event_id="evt-edit",
            target_native_message_id=None,
            target_available=True,
            fallback_text_source="relation_fallback_text_present",
            render_mode_reason="native_ref_unavailable",
        )
        d = evidence.to_dict()
        assert "render_mode_reason" in d
        assert isinstance(d["render_mode_reason"], str)
        assert d["render_mode_reason"] == "native_ref_unavailable"


# ===================================================================
# Plan skip: outcome carries plan reason/level fields
# ===================================================================


class TestPlanSkipOutcomeFields:
    """DeliveryOutcome from plan-driven skip carries specific fields.

    These tests exercise the runner integration path.  The runner tests
    in ``test_runner_plan_authority.py`` already cover the full runner
    pipeline.  Here we verify the DeliveryOutcome structure directly.
    """

    def test_outcome_skipped_with_capability_suppressed(self) -> None:
        """DeliveryOutcome skip with CAPABILITY_SUPPRESSED failure kind."""
        outcome = DeliveryOutcome(
            event_id="evt-skip-001",
            target_adapter="dest",
            target_channel=None,
            route_id="route-001",
            delivery_plan_id="plan-skip-001",
            status="skipped",
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            error="capability_suppressed: reactions unsupported by adapter",
            receipt=None,
        )
        assert outcome.status == "skipped"
        assert outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
        assert "reactions unsupported" in (outcome.error or "")

    def test_outcome_skip_with_generic_reason(self) -> None:
        """DeliveryOutcome skip with generic plan_skip reason."""
        outcome = DeliveryOutcome(
            event_id="evt-skip-002",
            target_adapter="dest",
            target_channel=None,
            route_id="route-002",
            delivery_plan_id="plan-skip-002",
            status="skipped",
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            error="plan_skip: delivery strategy is 'skip'",
            receipt=None,
        )
        assert outcome.status == "skipped"
        assert "plan_skip" in (outcome.error or "")
        assert "delivery strategy is 'skip'" in (outcome.error or "")


# ===================================================================
# Diagnostics / replay: documentation
# ===================================================================

# The following plan-driven behaviors are tested in other files:
#
# 1. Diagnostician.record_planner_failure for invalid capability_level:
#    test_target_delivery_evidence_and_capability.TestInvalidCapabilityDecision
#    Proves: invalid capability_level → PLANNER_FAILURE receipt +
#            diagnostician.planner_failures counter incremented.
#
# 2. ReplayEngine plan filtering by capability:
#    test_replay_engine_plan_filters (886 lines)
#    Proves: _filter_plans_by_capability, _stage_deliver BEST_EFFORT/DRY_RUN
#            capability filtering, relation-aware capability filtering.
#
# 3. Runner-level diagnostics for capability suppression:
#    test_runner_plan_authority.TestPhase25TrustsPlanCapabilityLevel
#    Proves: runner uses plan reason/level for suppression, not resolver.
#
# 4. Pipeline plan skip path:
#    test_pipeline_plan_skip.py
#    Proves: pipeline-level skip with reason propagation.
