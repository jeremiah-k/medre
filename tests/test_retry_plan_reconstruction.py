"""Focused tests for retry delivery-plan reconstruction.

Tests exercise the ``reconstruct_retry_delivery_plan`` helper in isolation,
verifying that persisted outbox/receipt data roundtrips into the correct
reconstructed route, plan, and retry policy — and that omitted fields are
intentionally absent (not guessed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.engine.pipeline.retry_plan import (
    ReconstructedRetryPlan,
    reconstruct_retry_delivery_plan,
)
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryStrategy,
    RetryPolicy,
    delivery_target_identity,
)
from medre.core.routing.models import Route, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_outbox(
    *,
    target_adapter: str = "matrix",
    target_channel: str | None = "#general",
    route_id: str = "route-1",
    delivery_plan_id: str = "plan-abc",
    event_id: str = "evt-001",
    metadata: dict | None = None,
) -> DeliveryOutboxItem:
    """Create a minimal outbox item for testing."""
    return DeliveryOutboxItem(
        outbox_id="ob-1",
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        metadata=metadata,
    )


def _make_receipt(
    *,
    retry_max_attempts: int | None = 5,
    retry_backoff_base: float | None = 3.0,
    retry_max_delay: float | None = 120.0,
    retry_jitter: bool | None = True,
) -> DeliveryReceipt:
    """Create a minimal receipt for testing."""
    return DeliveryReceipt(
        sequence=1,
        receipt_id="rcpt-1",
        event_id="evt-001",
        delivery_plan_id="plan-abc",
        target_adapter="matrix",
        status="failed",
        created_at=datetime.now(timezone.utc),
        attempt_number=1,
        retry_max_attempts=retry_max_attempts,
        retry_backoff_base=retry_backoff_base,
        retry_max_delay=retry_max_delay,
        retry_jitter=retry_jitter,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouteAndPlanIdPreservation:
    """route_id and delivery_plan_id are preserved through reconstruction."""

    def test_route_id_preserved(self) -> None:
        item = _make_outbox(route_id="my-route-42")
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.route.id == "my-route-42"
        assert ctx.plan.route_id == "my-route-42"

    def test_delivery_plan_id_preserved(self) -> None:
        item = _make_outbox(delivery_plan_id="plan-xyz")
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.plan_id == "plan-xyz"

    def test_empty_route_id_falls_back_to_empty_string_for_route(self) -> None:
        item = _make_outbox(route_id="")
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.route.id == ""
        # Plan route_id is None for empty route_id
        assert ctx.plan.route_id is None

    def test_event_id_preserved(self) -> None:
        item = _make_outbox(event_id="evt-999")
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.event_id == "evt-999"


class TestTargetAdapterChannel:
    """Target adapter and channel are preserved from the outbox item."""

    def test_adapter_channel_preserved(self) -> None:
        item = _make_outbox(target_adapter="lxmf", target_channel="mesh-1")
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        target = ctx.plan.target
        assert target.adapter == "lxmf"
        assert target.channel == "mesh-1"

    def test_channel_none_preserved(self) -> None:
        item = _make_outbox(target_channel=None)
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.target.channel is None


class TestDestinationMetadataRoundtrip:
    """Destination metadata from item.metadata roundtrips through reconstruction."""

    def test_destination_reconstructed_from_metadata(self) -> None:
        item = _make_outbox(
            metadata={
                "destination_kind": "matrix_room",
                "destination_hash": "abc123",
                "destination_name": "my-room",
                "destination_metadata": {"room_id": "!room:server"},
            }
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        dest = ctx.plan.target.destination
        assert dest is not None
        assert dest.kind == "matrix_room"
        assert dest.destination_hash == "abc123"
        assert dest.destination_name == "my-room"
        assert dest.metadata == {"room_id": "!room:server"}

    def test_no_destination_when_metadata_missing(self) -> None:
        item = _make_outbox(metadata=None)
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.target.destination is None

    def test_no_destination_when_destination_kind_absent(self) -> None:
        item = _make_outbox(metadata={"destination_hash": "abc"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.target.destination is None

    def test_destination_defaults_empty_metadata(self) -> None:
        item = _make_outbox(
            metadata={
                "destination_kind": "lxmf_destination",
            }
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        dest = ctx.plan.target.destination
        assert dest is not None
        assert dest.destination_hash is None
        assert dest.destination_name is None
        assert dest.metadata == {}


class TestRetryPolicyFromReceipt:
    """Retry policy is reconstructed from previous receipt fields."""

    def test_policy_from_receipt(self) -> None:
        item = _make_outbox()
        receipt = _make_receipt(
            retry_max_attempts=7,
            retry_backoff_base=5.0,
            retry_max_delay=300.0,
            retry_jitter=True,
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=receipt,
            default_max_attempts=3,
        )
        assert ctx.retry_policy == RetryPolicy(
            max_attempts=7,
            backoff_base=5.0,
            max_delay_seconds=300.0,
            jitter=True,
        )
        assert ctx.plan.retry_policy == ctx.retry_policy

    def test_receipt_null_fields_fall_back_to_defaults(self) -> None:
        item = _make_outbox()
        receipt = _make_receipt(
            retry_max_attempts=None,
            retry_backoff_base=None,
            retry_max_delay=None,
            retry_jitter=None,
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=receipt,
            default_max_attempts=10,
        )
        # max_attempts falls back to default_max_attempts, not hardcoded 3
        assert ctx.retry_policy.max_attempts == 10
        assert ctx.retry_policy.backoff_base == 2.0
        assert ctx.retry_policy.max_delay_seconds == 60.0
        assert ctx.retry_policy.jitter is False


class TestMissingReceiptDefaults:
    """Missing previous receipt falls back to hardcoded defaults."""

    def test_no_receipt_uses_default_max_attempts(self) -> None:
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=8,
        )
        assert ctx.retry_policy.max_attempts == 8

    def test_no_receipt_uses_hardcoded_backoff(self) -> None:
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.retry_policy.backoff_base == 2.0
        assert ctx.retry_policy.max_delay_seconds == 60.0
        assert ctx.retry_policy.jitter is False


class TestCapabilityFieldsUnreconstructed:
    """Capability fields are intentionally None; strategy is always direct."""

    def test_capability_fields_are_none(self) -> None:
        """Capability decisions are not persisted, so reconstruction cannot
        recover them.  This is documented intentional behavior."""
        item = _make_outbox()
        receipt = _make_receipt()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=receipt,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_level is None
        assert ctx.plan.capability_field is None
        assert ctx.plan.capability_reason is None

    def test_strategy_is_direct(self) -> None:
        """The original strategy is not persisted; retry always uses direct
        delivery — the standard rendering/adapter path."""
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")

    def test_fallback_chain_is_empty(self) -> None:
        """Fallback chains are not persisted and cannot be recovered."""
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.fallback_chain == []

    def test_deadline_is_none(self) -> None:
        """Deadline is not persisted and cannot be recovered."""
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.deadline is None


class TestTargetIdentityRecomputed:
    """target_identity is recomputed from the reconstructed target."""

    def test_identity_matches_target(self) -> None:
        item = _make_outbox(
            target_adapter="matrix",
            target_channel="#test",
            metadata={
                "destination_kind": "matrix_room",
                "destination_hash": "h1",
                "destination_name": "room",
                "destination_metadata": {"x": "1"},
            },
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        expected = delivery_target_identity(ctx.plan.target)
        assert ctx.plan.target_identity == expected

    def test_identity_changes_with_different_target(self) -> None:
        """Two different targets produce different identities."""
        item_a = _make_outbox(target_adapter="matrix", target_channel="#a")
        item_b = _make_outbox(target_adapter="matrix", target_channel="#b")
        ctx_a = reconstruct_retry_delivery_plan(
            item=item_a,
            previous_receipt=None,
            default_max_attempts=3,
        )
        ctx_b = reconstruct_retry_delivery_plan(
            item=item_b,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx_a.plan.target_identity != ctx_b.plan.target_identity


class TestReturnType:
    """The helper returns the correct frozen dataclass."""

    def test_returns_reconstructed_retry_plan(self) -> None:
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert isinstance(ctx, ReconstructedRetryPlan)
        assert isinstance(ctx.route, Route)
        assert isinstance(ctx.plan.target, RouteTarget)
        assert isinstance(ctx.retry_policy, RetryPolicy)

    def test_result_is_frozen(self) -> None:
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        with pytest.raises(AttributeError):
            ctx.route = ctx.route  # type: ignore[misc]
