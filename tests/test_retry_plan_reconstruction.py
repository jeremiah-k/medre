"""Focused tests for retry delivery-plan reconstruction.

Tests exercise the ``reconstruct_retry_delivery_plan`` helper in isolation,
verifying that persisted outbox/receipt data roundtrips into the correct
reconstructed route, plan, and retry policy — and that omitted fields are
intentionally absent (not guessed).
"""

from __future__ import annotations

import datetime
import logging

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
        created_at=datetime.datetime.now(datetime.UTC),
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


class TestCapabilityFieldsRoundtrip:
    """Capability and strategy fields are recovered from outbox metadata."""

    def test_capability_level_recovered(self) -> None:
        """capability_level persisted in metadata is recovered on reconstruction."""
        item = _make_outbox(
            metadata={
                "capability_level": "fallback",
                "delivery_strategy": "fallback_text",
            }
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_level == "fallback"

    def test_invalid_capability_level_falls_back_to_none(self) -> None:
        """Invalid capability_level in metadata degrades to None."""
        item = _make_outbox(metadata={"capability_level": "bogus"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_level is None

    def test_invalid_capability_level_logs_warning(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Invalid capability_level produces a warning log with context."""
        item = _make_outbox(
            metadata={"capability_level": "partial"},
        )
        with caplog.at_level(logging.WARNING):
            ctx = reconstruct_retry_delivery_plan(
                item=item,
                previous_receipt=None,
                default_max_attempts=3,
            )
        assert ctx.plan.capability_level is None
        assert "Invalid capability_level" in caplog.text
        assert "partial" in caplog.text
        assert "ob-1" in caplog.text

    def test_valid_capability_levels_pass_through(self) -> None:
        """Each valid capability_level value passes validation unchanged."""
        for level in ("native", "fallback", "unsupported"):
            item = _make_outbox(metadata={"capability_level": level})
            ctx = reconstruct_retry_delivery_plan(
                item=item,
                previous_receipt=None,
                default_max_attempts=3,
            )
            assert ctx.plan.capability_level == level

    def test_capability_field_recovered(self) -> None:
        """capability_field persisted in metadata is recovered on reconstruction."""
        item = _make_outbox(metadata={"capability_field": "reactions"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_field == "reactions"

    def test_capability_reason_recovered(self) -> None:
        """capability_reason persisted in metadata is recovered on reconstruction."""
        item = _make_outbox(
            metadata={"capability_reason": "reactions unsupported by adapter"}
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_reason == "reactions unsupported by adapter"

    def test_delivery_strategy_recovered(self) -> None:
        """delivery_strategy persisted in metadata is recovered on reconstruction."""
        item = _make_outbox(metadata={"delivery_strategy": "fallback_text"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="fallback_text")

    def test_deadline_recovered(self) -> None:
        """deadline persisted as ISO string is recovered on reconstruction."""
        deadline_str = "2026-12-31T23:59:59+00:00"
        item = _make_outbox(metadata={"deadline": deadline_str})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.deadline is not None
        assert ctx.plan.deadline.isoformat() == deadline_str

    def test_all_none_when_no_metadata(self) -> None:
        """Legacy outbox rows without route-decision metadata degrade gracefully."""
        item = _make_outbox(metadata=None)
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_level is None
        assert ctx.plan.capability_field is None
        assert ctx.plan.capability_reason is None
        assert ctx.plan.deadline is None
        # Default strategy is "direct" when metadata is absent.
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")

    def test_all_none_when_metadata_empty_dict(self) -> None:
        """Empty metadata dict degrades gracefully to defaults."""
        item = _make_outbox(metadata={})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.capability_level is None
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")

    def test_unknown_strategy_ignored(self) -> None:
        """Unknown delivery_strategy values fall back to 'direct'."""
        item = _make_outbox(metadata={"delivery_strategy": "unknown_method"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")

    def test_invalid_deadline_ignored(self) -> None:
        """Invalid deadline string falls back to None."""
        item = _make_outbox(metadata={"deadline": "not-a-date"})
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.deadline is None

    def test_fallback_chain_still_empty(self) -> None:
        """Fallback chains are not persisted and cannot be recovered."""
        item = _make_outbox()
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        assert ctx.plan.fallback_chain == []

    def test_full_roundtrip_with_destination_and_route_decision(self) -> None:
        """Full roundtrip: destination + capability + strategy + deadline."""
        item = _make_outbox(
            metadata={
                # Destination keys
                "destination_kind": "matrix_room",
                "destination_hash": "abc123",
                "destination_name": "my-room",
                "destination_metadata": {"room_id": "!room:server"},
                # Route-decision keys
                "capability_level": "native",
                "delivery_strategy": "direct",
                "capability_field": None,
                "capability_reason": None,
                "deadline": None,
            }
        )
        ctx = reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
        # Destination preserved
        dest = ctx.plan.target.destination
        assert dest is not None
        assert dest.kind == "matrix_room"
        assert dest.destination_hash == "abc123"
        # Capability/strategy preserved
        assert ctx.plan.capability_level == "native"
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")
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
