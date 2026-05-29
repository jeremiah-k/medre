"""Tests for dead-letter and suppression receipt creation and persistence.

Exercises ``build_and_persist_dead_letter_receipt`` and
``build_and_persist_suppression_receipt`` with real storage.
"""

from __future__ import annotations

import pytest

from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryPolicy,
)
from medre.core.storage.backend import StorageBackend

from .conftest import _make_lifecycle, _make_plan

# ===================================================================
# Dead-letter receipt creation (integration with real storage)
# ===================================================================


class TestBuildAndPersistDeadLetterReceipt:
    """Verify dead-letter receipt construction and persistence."""

    async def test_dead_letter_receipt_persisted(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Dead-letter receipt is appended to storage."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)

        receipt = await lifecycle.build_and_persist_dead_letter_receipt(
            temp_storage,
            event_id="evt-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            previous_receipt_id="rcpt-primary",
            attempt_number=1,
            error="boom",
            source="live",
            replay_run_id=None,
            target_channel=None,
            plan=plan,
        )

        assert receipt.status == "dead_lettered"
        assert receipt.parent_receipt_id == "rcpt-primary"
        assert receipt.attempt_number == 2  # attempt_number + 1
        stored = await temp_storage.list_receipts_for_event("evt-001")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_dead_letter_receipt_with_replay(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Dead-letter receipt preserves replay_run_id and source."""
        lifecycle = _make_lifecycle()
        policy = RetryPolicy(max_attempts=1)
        plan = _make_plan(retry_policy=policy)

        receipt = await lifecycle.build_and_persist_dead_letter_receipt(
            temp_storage,
            event_id="evt-002",
            delivery_plan_id="plan-002",
            target_adapter="mesh",
            previous_receipt_id="rcpt-orig",
            attempt_number=3,
            error="exhausted",
            source="replay",
            replay_run_id="run-42",
            target_channel="ch-0",
            plan=plan,
        )

        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-42"
        assert receipt.target_channel == "ch-0"
        assert receipt.attempt_number == 4


# ===================================================================
# Suppression receipt creation (integration with real storage)
# ===================================================================


class TestBuildAndPersistSuppressionReceipt:
    """Verify suppression receipt construction and persistence."""

    async def test_suppression_receipt_persisted(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Suppression receipt is appended to storage."""
        lifecycle = _make_lifecycle()

        receipt = await lifecycle.build_and_persist_suppression_receipt(
            temp_storage,
            event_id="evt-001",
            delivery_plan_id="plan-001",
            target_adapter="test_adapter",
            target_channel=None,
            route_id="route-001",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error="loop_prevented",
        )

        assert receipt.status == "suppressed"
        assert receipt.failure_kind == "loop_suppressed"
        assert receipt.attempt_number == 1
        assert receipt.parent_receipt_id is None
        assert receipt.next_retry_at is None
        stored = await temp_storage.list_receipts_for_event("evt-001")
        assert len(stored) == 1
        assert stored[0].receipt_id == receipt.receipt_id

    async def test_suppression_receipt_with_replay(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Suppression receipt preserves replay context."""
        lifecycle = _make_lifecycle()

        receipt = await lifecycle.build_and_persist_suppression_receipt(
            temp_storage,
            event_id="evt-002",
            delivery_plan_id="plan-002",
            target_adapter="dest",
            target_channel="ch-1",
            route_id="route-002",
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            error="blocked",
            source="replay",
            replay_run_id="run-99",
        )

        assert receipt.source == "replay"
        assert receipt.replay_run_id == "run-99"
        assert receipt.target_channel == "ch-1"


# ===================================================================
# Dead-letter receipt — runtime guard for missing retry_policy
# ===================================================================


class TestDeadLetterReceiptRuntimeGuard:
    """Verify build_and_persist_dead_letter_receipt raises without retry_policy."""

    async def test_raises_runtime_error_without_retry_policy(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Calling with plan.retry_policy=None raises RuntimeError."""
        lifecycle = _make_lifecycle()
        plan = _make_plan(retry_policy=None)

        with pytest.raises(RuntimeError, match="retry_policy"):
            await lifecycle.build_and_persist_dead_letter_receipt(
                temp_storage,
                event_id="evt-guard",
                delivery_plan_id="plan-guard",
                target_adapter="test_adapter",
                previous_receipt_id="rcpt-prev",
                attempt_number=1,
                error="boom",
                source="live",
                replay_run_id=None,
                target_channel=None,
                plan=plan,
            )

        # No receipt should have been persisted.
        stored = await temp_storage.list_receipts_for_event("evt-guard")
        assert len(stored) == 0
