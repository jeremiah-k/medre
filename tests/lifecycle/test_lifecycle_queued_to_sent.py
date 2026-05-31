"""Tests for supplemental queued→sent receipt generation.

Exercises ``append_queued_to_sent_receipt`` including happy paths,
outbox transitions, error handling, and deterministic plan_id
correlation (Deterministic plan_id correlation regression).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_lifecycle, _make_receipt

# ===================================================================
# Supplemental queued→sent receipt — happy paths
# ===================================================================


class TestAppendQueuedToSentReceipt:
    """Verify supplemental queued→sent receipt generation."""

    async def test_supplemental_sent_receipt_created(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Callback with matching queued receipt → supplemental sent receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-queued",
            status="queued",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-q",
        )
        await temp_storage.append_receipt(queued)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-42",
            delivery_plan_id="plan-q",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Should have 2 receipts now: original queued + supplemental sent.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-queued"
        assert sent[0].adapter_message_id == "packet-42"
        assert sent[0].delivery_plan_id == "plan-q"


# ===================================================================
# Same-channel retry lineage regression
# ===================================================================


class TestSameChannelRetryLineageRegression:
    """Regression tests for same-channel retry lineage when
    native_channel_id is missing.

    These tests verify that ``append_queued_to_sent_receipt`` correctly
    resolves unambiguous same-channel retry lineages and correctly
    rejects cross-channel ambiguity for the deterministic plan_id path.
    """

    async def test_plan_id_no_channel_same_plan_same_channel_multiple_attempts(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """(A) delivery_plan_id present + no native_channel_id + same plan +
        same channel + multiple attempts → supplemental sent receipt, parent
        latest queued attempt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Multiple retry attempts on same plan, same channel.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-a1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-r",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-a2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-r",
                attempt_number=2,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-a",
            delivery_plan_id="plan-r",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-a2"
        assert sent[0].attempt_number == 2

    async def test_plan_id_no_channel_same_plan_multiple_channels_skip(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(B) delivery_plan_id present + no native_channel_id + same plan +
        multiple channels → no supplemental receipt, warning logged."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-b0",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-bx",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-b1",
                status="queued",
                adapter="m",
                channel="1",
                plan_id="plan-bx",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-b",
            delivery_plan_id="plan-bx",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert "Ambiguous queued receipt correlation" in caplog.text
        assert "plan-bx" in caplog.text


# ===================================================================
# Source-aware candidate selection
# ===================================================================


class TestSourceAwareCandidateSelection:
    """Verify that append_queued_to_sent_receipt prefers non-replay queued
    receipts over replay queued receipts when multiple candidates match the
    same (delivery_plan_id, adapter, channel).

    This hardens queued-to-sent correlation against replay/live source
    contamination.
    """

    async def test_live_callback_prefers_live_queued_over_replay(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Live and replay queued receipts for same plan/channel → live wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Replay queued receipt (appended first).
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-replay",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-src",
                source="replay",
                replay_run_id="run-42",
            )
        )
        # Live queued receipt (appended second — would also win by recency).
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-live",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-src",
                source="live",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-live",
            delivery_plan_id="plan-src",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-live"
        assert sent[0].source == "live"
        assert sent[0].replay_run_id is None

    async def test_live_callback_prefers_live_even_if_replay_is_newer(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Replay receipt appended after live → live still wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Live queued receipt (appended first).
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-live-early",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-order",
                source="live",
            )
        )
        # Replay queued receipt (appended second — would win by recency alone).
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-replay-late",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-order",
                source="replay",
                replay_run_id="run-99",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-order",
            delivery_plan_id="plan-order",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-live-early"
        assert sent[0].source == "live"

    async def test_replay_only_candidate_skipped_with_warning(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only replay queued receipt exists → skipped with warning, no sent receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-replay-only",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-replay-only",
                source="replay",
                replay_run_id="run-77",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-replay",
            delivery_plan_id="plan-replay-only",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        # No supplemental sent receipt — replay-only candidate is skipped.
        assert len(sent) == 0
        # The original queued receipt remains untouched.
        queued = [r for r in all_receipts if r.status == "queued"]
        assert len(queued) == 1
        assert queued[0].receipt_id == "rcpt-replay-only"
        assert "only replay-sourced queued receipt" in caplog.text
        assert "skipping replay candidate" in caplog.text

    async def test_normal_live_only_unchanged(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Single live queued receipt → unchanged behaviour (regression)."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-live-single",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-live",
                source="live",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-plain",
            delivery_plan_id="plan-live",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-live-single"
        assert sent[0].source == "live"
        assert sent[0].replay_run_id is None

    async def test_repeated_callback_creates_append_only_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Repeated callback creates a second supplemental receipt (append-only).

        Receipts are immutable: the original queued receipt is never
        consumed or status-changed, so ``status == "queued"`` always
        matches on subsequent callbacks and a new supplemental receipt is
        appended each time.  This is the existing MEDRE behaviour — not
        idempotent, but outbox transition is idempotent (queued→sent
        only fires once).
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-dup",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-dup",
                source="live",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-1",
            delivery_plan_id="plan-dup",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Second callback targeting the same immutable queued receipt
        # produces another supplemental sent receipt.
        record2 = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-2",
            delivery_plan_id="plan-dup",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record2,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 2

    async def test_source_preference_no_channel_same_plan(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No native_channel_id on record, same plan, same channel, live+replay
        → live wins (exercises plan_matches path without channel filter)."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Replay candidate first.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-r1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-nc",
                source="replay",
                replay_run_id="run-10",
            )
        )
        # Live candidate second.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-l1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-nc",
                source="live",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-nc",
            delivery_plan_id="plan-nc",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-l1"
        assert sent[0].source == "live"

    async def test_multiple_replay_candidates_skipped_with_warning(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Multiple replay candidates, no live → all skipped with warning."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-rp1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-rmulti",
                source="replay",
                replay_run_id="run-a",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-rp2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-rmulti",
                source="replay",
                replay_run_id="run-b",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-rmulti",
            delivery_plan_id="plan-rmulti",
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        # No supplemental sent receipt — all replay candidates skipped.
        assert len(sent) == 0
        # Both queued receipts remain untouched.
        queued = [r for r in all_receipts if r.status == "queued"]
        assert len(queued) == 2
        assert "only replay-sourced queued receipts" in caplog.text
        assert "skipping all replay candidates" in caplog.text

    async def test_single_candidate_no_channel_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """One queued candidate + no channel on record → supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-only",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-single",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-single",
            delivery_plan_id="plan-single",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "pkt-single"

    async def test_retry_chooses_most_recent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Multiple queued receipts on same channel (retries) → last one wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-first",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-retry",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=2,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-retry",
            delivery_plan_id="plan-001",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-retry"
        assert sent[0].attempt_number == 2


# ===================================================================
# Supplemental queued→sent receipt — outbox transition
# ===================================================================


class TestSupplementalOutboxTransition:
    """Verify supplemental queued→sent receipt also transitions the outbox."""

    async def test_outbox_transitioned_from_queued_to_sent(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Supplemental receipt transitions matching outbox item queued→sent."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Pre-populate a queued receipt.
        queued = _make_receipt(
            receipt_id="rcpt-outbox-q",
            status="queued",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-outbox",
        )
        await temp_storage.append_receipt(queued)

        # Create a matching outbox item in "queued" status.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-supplemental",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-outbox",
            target_adapter="mesh-1",
            target_channel="0",
            status="queued",
        )
        await temp_storage.create_outbox_item(outbox_item)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-outbox-42",
            delivery_plan_id="plan-outbox",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Outbox should now be sent.
        updated = await temp_storage.get_outbox_item("obox-supplemental")
        assert updated is not None
        assert updated.status == "sent"

        # Supplemental sent receipt should exist.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].adapter_message_id == "packet-outbox-42"


# ===================================================================
# append_queued_to_sent_receipt — error paths
# ===================================================================


class TestAppendQueuedToSentErrorPaths:
    """Error paths in append_queued_to_sent_receipt."""

    async def test_list_receipts_error_logged(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """storage.list_receipts_for_event raises → logged, returns."""
        from unittest.mock import AsyncMock, patch

        lifecycle = _make_lifecycle()
        record = OutboundNativeRefRecord(
            event_id="evt-list-err",
            adapter="mesh-1",
            native_channel_id=None,
            native_message_id="pkt",
        )
        with patch.object(
            temp_storage,
            "list_receipts_for_event",
            AsyncMock(side_effect=RuntimeError("db fail")),
        ):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=datetime.now(timezone.utc),
            )
        assert "Failed to list receipts" in caplog.text

    async def test_channel_mismatch_skips_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Queued receipts exist but none match channel → skip."""
        lifecycle = _make_lifecycle()
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-ch", status="queued", adapter="m", channel="0"
            )
        )
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="1",
            native_message_id="pkt",
            delivery_plan_id="plan-001",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=datetime.now(timezone.utc),
        )
        all_r = await temp_storage.list_receipts_for_event("evt-001")
        assert all(r.status != "sent" for r in all_r)

    async def test_outbox_transition_error_logged(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """mark_outbox_sent raises → logged, does not propagate."""
        caplog.set_level(logging.DEBUG)
        from unittest.mock import AsyncMock, patch

        lifecycle = _make_lifecycle()
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-ob", status="queued", adapter="m", channel="0"
            )
        )

        # Mock get_outbox_item_for_delivery to return a dummy item so
        # the code path reaches mark_outbox_sent.  Then make
        # mark_outbox_sent raise to exercise the except block.
        dummy_item = DeliveryOutboxItem(
            outbox_id="obox-supp-err",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-q",
            target_adapter="m",
            target_channel="0",
            status="queued",
        )
        with (
            patch.object(
                temp_storage,
                "get_outbox_item_for_delivery",
                AsyncMock(return_value=dummy_item),
            ),
            patch.object(
                temp_storage,
                "mark_outbox_sent",
                AsyncMock(side_effect=RuntimeError("outbox write fail")),
            ),
        ):
            record = OutboundNativeRefRecord(
                event_id="evt-001",
                adapter="m",
                native_channel_id="0",
                native_message_id="pkt",
                delivery_plan_id="plan-001",
            )
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=datetime.now(timezone.utc),
            )
        assert "Failed to transition outbox queued->sent" in caplog.text


# ===================================================================
# Deterministic delivery_plan_id correlation regression
# ===================================================================


class TestDeterministicPlanIdCorrelation:
    """Regression tests for delivery_plan_id-based queued→sent correlation.

    These tests verify that ``append_queued_to_sent_receipt`` uses the
    ``delivery_plan_id`` on ``OutboundNativeRefRecord`` for deterministic
    correlation.  When ``delivery_plan_id`` is absent, no supplemental
    receipt is created.
    """

    async def test_overlapping_plans_same_channel_correct_receipt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Same event, same adapter, same channel, two different plan_ids.

        The supplemental sent receipt for plan-b must link to the
        plan-b queued receipt, not plan-a's.
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts: plan-a and plan-b, same adapter/channel.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-plan-a",
                status="queued",
                adapter="mesh",
                channel="0",
                plan_id="plan-a",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-plan-b",
                status="queued",
                adapter="mesh",
                channel="0",
                plan_id="plan-b",
            )
        )

        # Record for plan-b with delivery_plan_id set.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh",
            native_channel_id="0",
            native_message_id="pkt-plan-b",
            delivery_plan_id="plan-b",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        # Must parent the plan-b receipt, NOT plan-a.
        assert sent[0].parent_receipt_id == "rcpt-plan-b"
        assert sent[0].delivery_plan_id == "plan-b"
        assert sent[0].adapter_message_id == "pkt-plan-b"

        # plan-a's queued receipt must remain untouched (no sent receipt).
        plan_a_sent = [
            r
            for r in all_receipts
            if r.delivery_plan_id == "plan-a" and r.status == "sent"
        ]
        assert len(plan_a_sent) == 0

    async def test_overlapping_plans_both_receive_correct_receipts(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Both plan-a and plan-b receive correct supplemental receipts."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-a2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-a2",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-b2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-b2",
            )
        )

        # Record for plan-a2.
        record_a = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-a2",
            delivery_plan_id="plan-a2",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record_a,
            now=now,
        )

        # Record for plan-b2.
        record_b = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-b2",
            delivery_plan_id="plan-b2",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record_b,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 2

        sent_a = [r for r in sent if r.delivery_plan_id == "plan-a2"]
        assert len(sent_a) == 1
        assert sent_a[0].parent_receipt_id == "rcpt-a2"
        assert sent_a[0].adapter_message_id == "pkt-a2"

        sent_b = [r for r in sent if r.delivery_plan_id == "plan-b2"]
        assert len(sent_b) == 1
        assert sent_b[0].parent_receipt_id == "rcpt-b2"
        assert sent_b[0].adapter_message_id == "pkt-b2"

    async def test_retry_same_plan_selects_latest_attempt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Same plan, multiple queued attempts → latest wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-attempt1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-attempt2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry",
                attempt_number=2,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-latest",
            delivery_plan_id="plan-retry",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-attempt2"
        assert sent[0].attempt_number == 2

    async def test_plan_id_not_found_no_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """delivery_plan_id on record but no matching queued receipt → skip."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-x",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-x",
            )
        )

        # Record with a different plan_id that doesn't match.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-nope",
            delivery_plan_id="plan-nonexistent",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_plan_id_no_channel_multiple_plan_matches_skip(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """delivery_plan_id set, no channel, multiple plan matches → skip."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-ch0",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-multi",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-ch1",
                status="queued",
                adapter="m",
                channel="1",
                plan_id="plan-multi",
            )
        )

        # Same plan but no channel → ambiguous.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-ambig",
            delivery_plan_id="plan-multi",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_plan_id_nonexistent_with_heuristic_candidates_no_fallback(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """delivery_plan_id present but nonexistent, heuristic candidates exist
        → no supplemental receipt and no heuristic fallback."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Multiple queued receipts that WOULD match via heuristic.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-heur-a",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-real-a",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-heur-b",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-real-b",
            )
        )

        # Record with a plan_id that matches NONE of the queued receipts.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-ghost",
            delivery_plan_id="plan-nonexistent",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # No supplemental sent receipt — heuristic fallback must NOT be used.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_missing_delivery_plan_id_no_supplemental_receipt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record without delivery_plan_id, queued receipts exist → NO sent
        receipt created."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-no-plan",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-1",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-no-plan",
            # delivery_plan_id is None
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_missing_delivery_plan_id_logs_warning(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Record without delivery_plan_id logs at WARNING level with
        event_id, adapter, and native_channel_id context."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-no-plan-warn",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-warn",
                event_id="evt-warn",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-warn",
            adapter="mesh-1",
            native_channel_id="ch-warn-42",
            native_message_id="pkt-warn",
            # delivery_plan_id is None
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=now,
            )

        # Must be WARNING level, not DEBUG.
        warning_records = [
            r
            for r in caplog.records
            if "delivery_plan_id" in r.message and "not available" in r.message
        ]
        assert len(warning_records) >= 1
        assert warning_records[0].levelname == "WARNING"
        # Must include operator context.
        assert "evt-warn" in caplog.text
        assert "mesh-1" in caplog.text
        assert "ch-warn-42" in caplog.text
        assert "uncorrelated" in caplog.text

    async def test_missing_delivery_plan_id_no_outbox_transition(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record without delivery_plan_id → outbox not transitioned to sent."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-no-plan-ob",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-ob",
            )
        )

        # Create a matching outbox item in "queued" status.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-no-plan",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-ob",
            target_adapter="mesh-1",
            target_channel="0",
            status="queued",
        )
        await temp_storage.create_outbox_item(outbox_item)

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-no-plan-ob",
            # delivery_plan_id is None
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Outbox should still be queued (not sent).
        updated = await temp_storage.get_outbox_item("obox-no-plan")
        assert updated is not None
        assert updated.status == "queued"

    async def test_delivery_plan_id_mismatch_skips_correlation(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record has plan_id "plan-x" but no queued receipt with that plan →
        no sent receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-other",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-other",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-mismatch",
            delivery_plan_id="plan-x",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0

    async def test_delivery_plan_id_channel_match_still_works(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record has plan_id + channel, matching receipt exists → sent receipt
        created correctly."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-match",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-match",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-match-ok",
            delivery_plan_id="plan-match",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-match"
        assert sent[0].delivery_plan_id == "plan-match"
        assert sent[0].adapter_message_id == "pkt-match-ok"

    async def test_delivery_plan_id_channel_mismatch_skips(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record has plan_id + channel, receipt matches plan but NOT channel
        → no sent receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-ch-mismatch",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-ch",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="99",
            native_message_id="pkt-ch-mismatch",
            delivery_plan_id="plan-ch",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0


# ===================================================================
# delivery_state transition guard at queued→sent (D)
# ===================================================================


class TestDeliveryStateTransitionGuard:
    """Verify that append_queued_to_sent_receipt validates the selected
    queued receipt can transition to sent via delivery_state helper."""

    async def test_non_queued_status_skips_supplemental(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """A 'failed' receipt selected as candidate → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # A receipt with status='failed' (not 'queued').
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-failed",
                status="failed",
                adapter="m",
                channel="0",
                plan_id="plan-f",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-noop",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
