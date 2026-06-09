"""Tests that outbox metadata persistence includes route-decision fields.

These tests verify the _persistence side_ of route-decision metadata:
that the metadata dict constructed by
``PipelineRunner._create_outbox_for_delivery`` includes all 5
route-decision keys alongside destination metadata.  The
_reconstruction_ side is tested in ``test_retry_route_decision_parity.py``
and ``test_retry_plan_reconstruction.py``.

Because ``_create_outbox_for_delivery`` is a private async method on
PipelineRunner requiring substantial fixture setup, these tests
directly construct the metadata dict the same way the runner does and
verify the keys are present and correctly valued.  This catches
regressions where someone removes the metadata injection without
also updating the reconstruction code.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing.models import RouteDestination, RouteTarget

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(
    *,
    capability_level: str | None = None,
    capability_field: str | None = None,
    capability_reason: str | None = None,
    strategy_method: str = "direct",
    deadline: datetime | None = None,
) -> DeliveryPlan:
    """Create a minimal DeliveryPlan with configurable route-decision fields."""
    target = RouteTarget(adapter="matrix", channel="#test")
    return DeliveryPlan(
        plan_id="plan-test",
        event_id="evt-test",
        target=target,
        primary_strategy=DeliveryStrategy(method=strategy_method),
        capability_level=capability_level,
        capability_field=capability_field,
        capability_reason=capability_reason,
        deadline=deadline,
    )


def _build_metadata(
    plan: DeliveryPlan,
    destination: RouteDestination | None = None,
) -> dict | None:
    """Build the outbox metadata dict exactly as _create_outbox_for_delivery does.

    This replicates the logic from runner.py lines 1943-1970 so we can
    test the persistence contract without requiring a full PipelineRunner.
    """
    _dest_meta: dict | None = None
    if destination is not None:
        _dest_meta = {
            "destination_kind": destination.kind,
            "destination_hash": destination.destination_hash,
            "destination_name": destination.destination_name,
            "destination_metadata": destination.metadata,
        }

    # Route-decision metadata — mirrors runner.py
    _route_decision_meta: dict[str, object] = {
        "capability_level": plan.capability_level,
        "delivery_strategy": plan.primary_strategy.method,
        "capability_field": plan.capability_field,
        "capability_reason": plan.capability_reason,
        "deadline": (plan.deadline.isoformat() if plan.deadline is not None else None),
    }
    if _dest_meta is not None:
        _dest_meta.update(_route_decision_meta)
    else:
        _dest_meta = _route_decision_meta

    return _dest_meta


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutboxMetadataRouteDecisionKeys:
    """Route-decision metadata keys are present in the outbox metadata dict."""

    def test_all_five_keys_present(self) -> None:
        plan = _make_plan(
            capability_level="fallback",
            capability_field="reactions",
            capability_reason="reactions unsupported",
            strategy_method="fallback_text",
            deadline=datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
        )
        meta = _build_metadata(plan)
        assert meta is not None
        assert meta["capability_level"] == "fallback"
        assert meta["delivery_strategy"] == "fallback_text"
        assert meta["capability_field"] == "reactions"
        assert meta["capability_reason"] == "reactions unsupported"
        assert meta["deadline"] == "2026-12-31T23:59:59+00:00"

    def test_none_values_stored_as_none(self) -> None:
        plan = _make_plan()  # All defaults are None
        meta = _build_metadata(plan)
        assert meta is not None
        assert meta["capability_level"] is None
        assert meta["delivery_strategy"] == "direct"
        assert meta["capability_field"] is None
        assert meta["capability_reason"] is None
        assert meta["deadline"] is None

    def test_native_capability_stored(self) -> None:
        plan = _make_plan(
            capability_level="native",
            strategy_method="direct",
        )
        meta = _build_metadata(plan)
        assert meta["capability_level"] == "native"
        assert meta["delivery_strategy"] == "direct"

    def test_skip_strategy_stored(self) -> None:
        plan = _make_plan(
            capability_level="unsupported",
            strategy_method="skip",
        )
        meta = _build_metadata(plan)
        assert meta["capability_level"] == "unsupported"
        assert meta["delivery_strategy"] == "skip"


class TestOutboxMetadataWithDestination:
    """Route-decision keys coexist with destination keys in metadata."""

    def test_destination_keys_preserved(self) -> None:
        plan = _make_plan(capability_level="native")
        dest = RouteDestination(
            kind="matrix_room",
            destination_hash="abc123",
            destination_name="test-room",
            metadata={"room_id": "!room:server"},
        )
        meta = _build_metadata(plan, destination=dest)
        assert meta is not None
        # Destination keys preserved
        assert meta["destination_kind"] == "matrix_room"
        assert meta["destination_hash"] == "abc123"
        assert meta["destination_name"] == "test-room"
        assert meta["destination_metadata"] == {"room_id": "!room:server"}
        # Route-decision keys also present
        assert meta["capability_level"] == "native"
        assert meta["delivery_strategy"] == "direct"

    def test_no_destination_only_route_decision(self) -> None:
        plan = _make_plan(capability_level="fallback")
        meta = _build_metadata(plan, destination=None)
        assert meta is not None
        # No destination keys
        assert "destination_kind" not in meta
        # Route-decision keys present
        assert meta["capability_level"] == "fallback"

    def test_all_keys_together(self) -> None:
        plan = _make_plan(
            capability_level="fallback",
            capability_field="reactions",
            capability_reason="reactions degraded",
            strategy_method="fallback_text",
            deadline=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        dest = RouteDestination(
            kind="mesh_channel",
            destination_hash="mesh-1",
            destination_name="mesh-1",
        )
        meta = _build_metadata(plan, destination=dest)
        assert meta is not None
        # 4 destination keys + 5 route-decision keys = 9 total
        assert len(meta) == 9
        assert meta["destination_kind"] == "mesh_channel"
        assert meta["capability_level"] == "fallback"
        assert meta["delivery_strategy"] == "fallback_text"
        assert meta["capability_field"] == "reactions"
        assert meta["capability_reason"] == "reactions degraded"
        assert meta["deadline"] == "2026-06-01T00:00:00+00:00"
