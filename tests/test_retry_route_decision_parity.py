"""Behavioral tests for retry route-decision metadata parity.

Verifies that ``reconstruct_retry_delivery_plan()`` faithfully preserves
the original live delivery's route-decision metadata (capability_level,
delivery_strategy, capability_field, capability_reason, deadline) across
the outbox → reconstruction roundtrip.

These are *unit tests* of the reconstruction function — no PipelineRunner,
no adapters, no integration machinery.
"""

from __future__ import annotations

import datetime

import pytest

from medre.core.engine.pipeline.retry_plan import (
    reconstruct_retry_delivery_plan,
)
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import DeliveryStrategy
from medre.core.storage.backend import DeliveryOutboxItem

# ---------------------------------------------------------------------------
# Helpers (same pattern as test_retry_plan_reconstruction.py)
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


def _reconstruct(
    metadata: dict | None,
    *,
    previous_receipt: DeliveryReceipt | None = None,
    default_max_attempts: int = 3,
):
    """Shorthand: make outbox + reconstruct in one call."""
    item = _make_outbox(metadata=metadata)
    return reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=previous_receipt,
        default_max_attempts=default_max_attempts,
    )


# ===================================================================
# TestRetryPreservesCapabilityLevel
# ===================================================================


class TestRetryPreservesCapabilityLevel:
    """capability_level and delivery_strategy roundtrip through retry."""

    def test_fallback_capability_preserved_on_retry(self) -> None:
        ctx = _reconstruct(
            metadata={
                "capability_level": "fallback",
                "delivery_strategy": "fallback_text",
            }
        )
        assert ctx.plan.capability_level == "fallback"
        assert ctx.plan.primary_strategy.method == "fallback_text"

    def test_native_capability_preserved_on_retry(self) -> None:
        ctx = _reconstruct(
            metadata={
                "capability_level": "native",
                "delivery_strategy": "direct",
            }
        )
        assert ctx.plan.capability_level == "native"
        assert ctx.plan.primary_strategy.method == "direct"

    def test_unsupported_capability_preserved_on_retry(self) -> None:
        """Unsupported items wouldn't normally have outbox rows (Phase 2.5
        suppresses them), but reconstruction must still work in isolation."""
        ctx = _reconstruct(
            metadata={
                "capability_level": "unsupported",
                "delivery_strategy": "skip",
            }
        )
        assert ctx.plan.capability_level == "unsupported"
        assert ctx.plan.primary_strategy.method == "skip"


# ===================================================================
# TestRetryPreservesDeadline
# ===================================================================


class TestRetryPreservesDeadline:
    """deadline stored as ISO 8601 string roundtrips correctly."""

    def test_deadline_roundtrip(self) -> None:
        deadline_str = "2026-12-31T23:59:59+00:00"
        ctx = _reconstruct(metadata={"deadline": deadline_str})
        assert ctx.plan.deadline is not None
        assert ctx.plan.deadline.isoformat() == deadline_str

    def test_no_deadline_when_none(self) -> None:
        ctx = _reconstruct(metadata={"deadline": None})
        assert ctx.plan.deadline is None


# ===================================================================
# TestRetryLegacyMetadataDegradation
# ===================================================================


class TestRetryLegacyMetadataDegradation:
    """Legacy outbox rows without route-decision keys degrade gracefully."""

    def test_legacy_outbox_no_route_decision_keys(self) -> None:
        """Metadata with only destination keys (no capability_level,
        no delivery_strategy, no deadline) degrades to defaults."""
        ctx = _reconstruct(
            metadata={
                "destination_kind": "matrix_room",
                "destination_hash": "abc123",
                "destination_name": "my-room",
            }
        )
        assert ctx.plan.capability_level is None
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")
        assert ctx.plan.deadline is None

    def test_empty_metadata(self) -> None:
        ctx = _reconstruct(metadata={})
        assert ctx.plan.capability_level is None
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")
        assert ctx.plan.deadline is None

    def test_none_metadata(self) -> None:
        ctx = _reconstruct(metadata=None)
        assert ctx.plan.capability_level is None
        assert ctx.plan.primary_strategy == DeliveryStrategy(method="direct")
        assert ctx.plan.deadline is None


# ===================================================================
# TestRetryStrategyValidation
# ===================================================================


class TestRetryStrategyValidation:
    """delivery_strategy values are validated against the closed vocabulary."""

    @pytest.mark.parametrize(
        "method",
        [
            "direct",
            "fallback_text",
            "skip",
            "propagated",
            "opportunistic",
            "paper",
        ],
    )
    def test_known_strategy_values_accepted(self, method: str) -> None:
        ctx = _reconstruct(metadata={"delivery_strategy": method})
        assert ctx.plan.primary_strategy.method == method

    def test_unknown_strategy_falls_back_to_direct(self) -> None:
        ctx = _reconstruct(metadata={"delivery_strategy": "bogus_method"})
        assert ctx.plan.primary_strategy.method == "direct"


# ===================================================================
# TestRetryCapabilityFieldReasonRoundtrip
# ===================================================================


class TestRetryCapabilityFieldReasonRoundtrip:
    """capability_field and capability_reason are recovered from metadata."""

    def test_capability_field_roundtrip(self) -> None:
        ctx = _reconstruct(metadata={"capability_field": "reactions"})
        assert ctx.plan.capability_field == "reactions"

    def test_capability_reason_roundtrip(self) -> None:
        reason = "reactions unsupported by adapter (event_kind=message.reacted)"
        ctx = _reconstruct(metadata={"capability_reason": reason})
        assert ctx.plan.capability_reason == reason


# ===================================================================
# TestRetryFullRouteDecisionRoundtrip
# ===================================================================


class TestRetryFullRouteDecisionRoundtrip:
    """Full route-decision metadata roundtrips with all fields populated."""

    def test_full_roundtrip_all_fields(self) -> None:
        receipt = _make_receipt(
            retry_max_attempts=7,
            retry_backoff_base=5.0,
            retry_max_delay=300.0,
            retry_jitter=True,
        )
        ctx = _reconstruct(
            metadata={
                # Destination keys
                "destination_kind": "matrix_room",
                "destination_hash": "abc123",
                "destination_name": "my-room",
                "destination_metadata": {"room_id": "!room:server"},
                # Route-decision keys
                "capability_level": "fallback",
                "delivery_strategy": "fallback_text",
                "capability_field": "reactions",
                "capability_reason": (
                    "reactions unsupported by adapter" " (event_kind=message.reacted)"
                ),
                "deadline": "2026-06-01T00:00:00+00:00",
            },
            previous_receipt=receipt,
        )
        plan = ctx.plan

        # Destination
        dest = plan.target.destination
        assert dest is not None
        assert dest.kind == "matrix_room"

        # Capability level
        assert plan.capability_level == "fallback"

        # Strategy
        assert plan.primary_strategy.method == "fallback_text"

        # Field and reason
        assert plan.capability_field == "reactions"
        assert (
            plan.capability_reason
            == "reactions unsupported by adapter (event_kind=message.reacted)"
        )

        # Deadline
        assert plan.deadline is not None
        assert plan.deadline.isoformat() == "2026-06-01T00:00:00+00:00"

        # Retry policy from receipt
        assert ctx.retry_policy.max_attempts == 7
        assert ctx.retry_policy.backoff_base == 5.0
