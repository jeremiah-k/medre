"""Tests for outbox finalization via finalize_outbox_outcome.

Exercises status transitions, error swallowing, retry timestamp
alignment, and the defensive no-receipt fallback path.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryPolicy,
)
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_lifecycle, _make_receipt


# ===================================================================
# Outbox finalization — status transitions
# ===================================================================


class TestFinalizeOutboxOutcome:
    """Verify outbox finalization decisions."""

    async def test_no_outbox_skips(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No outbox_id → no action."""
        lifecycle = _make_lifecycle()
        # Should not raise.
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            None,
            False,
            None,
            None,
            None,
            None,
        )

    async def test_sent_receipt_marks_outbox_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Receipt with status='sent' → mark_outbox_sent."""
        lifecycle = _make_lifecycle()

        # Create an outbox item.
        item = DeliveryOutboxItem(
            outbox_id="obox-sent-test",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="sent")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-sent-test",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-sent-test")
        assert updated is not None
        assert updated.status == "sent"

    async def test_queued_receipt_marks_outbox_queued(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Receipt with status='queued' → mark_outbox_queued."""
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-queued-test",
            event_id="evt-q",
            route_id="route-q",
            delivery_plan_id="plan-q",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="queued", event_id="evt-q")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-queued-test",
            True,
            receipt,
            None,
            None,
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-queued-test")
        assert updated is not None
        assert updated.status == "queued"

    async def test_permanent_failure_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Permanent failure → mark_outbox_dead_lettered."""
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-dl-test",
            event_id="evt-dl",
            route_id="route-dl",
            delivery_plan_id="plan-dl",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="failed", event_id="evt-dl")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-dl-test",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_PERMANENT,
            "malformed",
            None,
        )

        updated = await temp_storage.get_outbox_item("obox-dl-test")
        assert updated is not None
        assert updated.status == "dead_lettered"

    async def test_retryable_failure_marks_retry_wait(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Retryable failure with policy -> mark_outbox_retry_wait."""
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-rw-test",
            event_id="evt-rw",
            route_id="route-rw",
            delivery_plan_id="plan-rw",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        retry_at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        receipt = _make_receipt(
            status="failed",
            event_id="evt-rw",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=retry_at,
        )
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-rw-test",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-rw-test")
        assert updated is not None
        assert updated.status == "retry_wait"

    async def test_retryable_no_policy_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Retryable failure without policy → dead_lettered (terminal)."""
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-rw-np",
            event_id="evt-rw-np",
            route_id="route-rw-np",
            delivery_plan_id="plan-rw-np",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        receipt = _make_receipt(status="failed", event_id="evt-rw-np")
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-rw-np",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            None,  # no retry policy → terminal
        )

        updated = await temp_storage.get_outbox_item("obox-rw-np")
        assert updated is not None
        assert updated.status == "dead_lettered"


# ===================================================================
# finalize_outbox_outcome — storage error swallowed
# ===================================================================


class TestFinalizeOutboxSwallowsStorageErrors:
    """Verify finalize_outbox_outcome logs and swallows storage exceptions."""

    async def test_storage_error_does_not_propagate(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Exception from storage.mark_outbox_sent is caught and logged."""
        from unittest.mock import AsyncMock

        lifecycle = _make_lifecycle()
        receipt = _make_receipt(status="sent")

        # Patch the storage to raise on mark_outbox_sent.
        temp_storage.mark_outbox_sent = AsyncMock(  # type: ignore[assignment]
            side_effect=RuntimeError("storage is offline")
        )

        # Should NOT raise despite the broken storage method.
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-broken",
            True,
            receipt,
            None,
            None,
            None,
        )

        # Verify the method was actually called.
        temp_storage.mark_outbox_sent.assert_awaited_once()  # type: ignore[attr-defined]


# ===================================================================
# finalize_outbox_outcome — retry timestamp alignment
# ===================================================================


class TestFinalizeOutboxRetryTimestampAlignment:
    """Verify finalize_outbox_outcome aligns outbox retry_wait with receipt."""

    async def test_exhausted_transient_with_policy_marks_dead_lettered(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Transient failure, retry policy, next_retry_at=None -> dead_lettered.

        When the receipt has status='failed', failure_kind=adapter_transient,
        a retry policy exists, but next_retry_at is None (exhausted), the
        outbox must be marked dead_lettered, not retry_wait.
        """
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-exhausted",
            event_id="evt-exhausted",
            route_id="route-ex",
            delivery_plan_id="plan-ex",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        # Receipt: failed, transient, next_retry_at=None (exhausted).
        receipt = _make_receipt(
            status="failed",
            event_id="evt-exhausted",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=None,
        )
        policy = RetryPolicy(max_attempts=1, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-exhausted",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-exhausted")
        assert updated is not None
        assert updated.status == "dead_lettered"

    async def test_retry_wait_uses_receipt_next_retry_at(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """retry_wait uses exact receipt.next_retry_at when present.

        When the receipt has a non-None next_retry_at, the outbox
        retry_wait next_attempt_at must match it exactly, not be
        recomputed from backoff.
        """
        lifecycle = _make_lifecycle()

        item = DeliveryOutboxItem(
            outbox_id="obox-aligned",
            event_id="evt-aligned",
            route_id="route-al",
            delivery_plan_id="plan-al",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        # Craft a receipt with a specific next_retry_at.
        expected_retry_at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        receipt = _make_receipt(
            status="failed",
            event_id="evt-aligned",
            failure_kind=DeliveryFailureKind.ADAPTER_TRANSIENT.value,
            next_retry_at=expected_retry_at,
        )
        policy = RetryPolicy(max_attempts=3, backoff_base=1.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-aligned",
            True,
            receipt,
            DeliveryFailureKind.ADAPTER_TRANSIENT,
            "timeout",
            policy,
        )

        updated = await temp_storage.get_outbox_item("obox-aligned")
        assert updated is not None
        assert updated.status == "retry_wait"
        # The outbox next_attempt_at should match receipt.next_retry_at
        # exactly (both are ISO-formatted from the same datetime).
        assert updated.next_attempt_at is not None
        assert updated.next_attempt_at == expected_retry_at.isoformat()


# ===================================================================
# finalize_outbox_outcome — defensive fallback (no receipt)
# ===================================================================


class TestFinalizeOutboxDefensiveFallback:
    """Defensive fallback in finalize_outbox_outcome when receipt is None."""

    async def test_defensive_backoff_when_no_receipt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Receipt None, retryable, policy exists → compute backoff from scratch."""
        lifecycle = _make_lifecycle()
        item = DeliveryOutboxItem(
            outbox_id="obox-fallback",
            event_id="evt-fallback",
            route_id="route-fb",
            delivery_plan_id="plan-fb",
            target_adapter="test_adapter",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(item)

        policy = RetryPolicy(max_attempts=5, backoff_base=2.0)
        await lifecycle.finalize_outbox_outcome(
            temp_storage,
            "obox-fallback",
            True,
            receipt=None,
            failure_kind_val=DeliveryFailureKind.ADAPTER_TRANSIENT,
            error="timeout",
            retry_policy=policy,
        )
        updated = await temp_storage.get_outbox_item("obox-fallback")
        assert updated is not None
        assert updated.status == "retry_wait"
        assert updated.next_attempt_at is not None
