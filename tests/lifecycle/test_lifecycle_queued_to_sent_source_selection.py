"""Tests for source-aware candidate selection in queued→sent correlation.

Exercises ``append_queued_to_sent_receipt`` source preference logic:
live queued receipts are preferred over replay queued receipts when
multiple candidates match the same (delivery_plan_id, adapter, channel).

Split from ``test_lifecycle_queued_to_sent.py`` (behavioral domain:
live vs replay source contamination hardening).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_lifecycle, _make_receipt

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

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-live-vs-replay",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-src",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-live-vs-replay")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-live",
            delivery_plan_id="plan-src",
            outbox_id="obox-live-vs-replay",
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

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-order",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-order",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-order")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-order",
            delivery_plan_id="plan-order",
            outbox_id="obox-order",
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
        # No supplemental sent receipt — hard reject fires (no outbox_id).
        assert len(sent) == 0
        # The original queued receipt remains untouched.
        queued = [r for r in all_receipts if r.status == "queued"]
        assert len(queued) == 1
        assert queued[0].receipt_id == "rcpt-replay-only"
        assert "Hard reject" in caplog.text
        assert "lacks outbox_id" in caplog.text
        # Must NOT log the spurious "Logic error" warning.
        assert "Logic error" not in caplog.text

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

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-live-single",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-live",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-live-single")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-plain",
            delivery_plan_id="plan-live",
            outbox_id="obox-live-single",
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

        # Create two outbox items with different attempt numbers so they
        # don't collide on the (plan, adapter, channel, attempt) unique key.
        outbox_item1 = DeliveryOutboxItem(
            outbox_id="obox-dup-1",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-dup",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item1)
        await temp_storage.mark_outbox_queued("obox-dup-1")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-1",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup-1",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        # Second callback: new outbox item for attempt 2 targeting the
        # same queued receipt (append-only: the queued receipt is never
        # consumed and still matches on subsequent callbacks).
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-dup-2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-dup",
                source="live",
                attempt_number=2,
            )
        )
        outbox_item2 = DeliveryOutboxItem(
            outbox_id="obox-dup-2",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-dup",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await temp_storage.create_outbox_item(outbox_item2)
        await temp_storage.mark_outbox_queued("obox-dup-2")

        record2 = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-2",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup-2",
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

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-nc",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-nc",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-nc")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-nc",
            delivery_plan_id="plan-nc",
            outbox_id="obox-nc",
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
        # No supplemental sent receipt — hard reject fires (no outbox_id).
        assert len(sent) == 0
        # Both queued receipts remain untouched.
        queued = [r for r in all_receipts if r.status == "queued"]
        assert len(queued) == 2
        assert "Hard reject" in caplog.text
        assert "lacks outbox_id" in caplog.text
        # Must NOT log the spurious "Logic error" warning.
        assert "Logic error" not in caplog.text

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

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-single",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-single",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-single")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-single",
            delivery_plan_id="plan-single",
            outbox_id="obox-single",
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

        # Create matching outbox item for exact correlation (attempt 2).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-retry",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-001",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-retry",
            delivery_plan_id="plan-001",
            outbox_id="obox-retry",
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
