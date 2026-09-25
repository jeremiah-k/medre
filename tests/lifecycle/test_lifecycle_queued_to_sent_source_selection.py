"""Tests for source-aware candidate selection in queued→sent correlation.

Exercises ``finalize_queued_delivery`` source preference logic:
live queued receipts are preferred over replay queued receipts when
multiple candidates match the same (delivery_plan_id, adapter, channel).

Split from ``test_lifecycle_queued_to_sent.py`` (behavioral domain:
live vs replay source contamination hardening).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.events import DeliveryAttemptProvenance
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from medre.core.storage.sqlite.constants import STALE_QUEUED_GRACE_SECONDS
from tests.helpers.delivery_callbacks import make_terminal_record
from tests.helpers.storage_outbox import (
    append_receipt_with_parent,
    create_outbox_item_with_parent,
)

from .conftest import _make_lifecycle, _make_receipt

# ===================================================================
# Source-aware candidate selection
# ===================================================================


class TestSourceAwareCandidateSelection:
    """Verify that finalize_queued_delivery prefers non-replay queued
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
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-replay",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-src",
                source="replay",
                replay_run_id="run-42",
                outbox_id="obox-live-vs-replay",
            ),
        )
        # Live queued receipt (appended second — would also win by recency).
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-live",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-src",
                source="live",
                outbox_id="obox-live-vs-replay",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-live-vs-replay")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-live",
            delivery_plan_id="plan-src",
            outbox_id="obox-live-vs-replay",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-live-early",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-order",
                source="live",
                outbox_id="obox-order",
            ),
        )
        # Replay queued receipt (appended second — would win by recency alone).
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-replay-late",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-order",
                source="replay",
                replay_run_id="run-99",
                outbox_id="obox-order",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-order")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-order",
            delivery_plan_id="plan-order",
            outbox_id="obox-order",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-live-early"
        assert sent[0].source == "live"

    async def test_replay_only_candidate_finalizes_with_replay_lineage(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Replay queued receipt for the exact callback row → sent with the
        replay lineage carried from the durable queued receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-replay-only",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-replay-only",
                source="replay",
                replay_run_id="run-77",
                outbox_id="obox-replay-only",
                attempt_number=1,
            ),
        )
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-replay-only",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-replay-only",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-replay-only")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-replay",
            delivery_plan_id="plan-replay-only",
            outbox_id="obox-replay-only",
            attempt_number=1,
        )
        with caplog.at_level(logging.DEBUG):
            await lifecycle.finalize_queued_delivery(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        # The supplemental sent receipt carries the replay lineage of the
        # exact queued receipt it finalizes — no live provenance fabricated.
        assert sent[0].source == "replay"
        assert sent[0].replay_run_id == "run-77"
        assert sent[0].parent_receipt_id == "rcpt-replay-only"
        assert sent[0].attempt_number == 1
        assert sent[0].adapter_message_id == "pkt-replay"
        assert sent[0].outbox_id == "obox-replay-only"
        # The outbox row reached the terminal sent state at callback time.
        outbox = await temp_storage.get_outbox_item("obox-replay-only")
        assert outbox is not None
        assert outbox.status == "sent"
        assert "selecting replay-sourced queued receipt rcpt-replay-only" in caplog.text
        assert "replay_run_id=run-77" in caplog.text
        assert "skipping" not in caplog.text
        assert "Hard reject" not in caplog.text

    async def test_normal_live_only_unchanged(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Single live queued receipt → unchanged behaviour (regression)."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-live-single",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-live",
                source="live",
                outbox_id="obox-live-single",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-live-single")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-plain",
            delivery_plan_id="plan-live",
            outbox_id="obox-live-single",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-dup",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-dup",
                source="live",
                outbox_id="obox-dup-1",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item1)
        await temp_storage.mark_outbox_queued("obox-dup-1")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-1",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup-1",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=now,
        )

        # Second callback: new outbox item for attempt 2 targeting the
        # same queued receipt (append-only: the queued receipt is never
        # consumed and still matches on subsequent callbacks).
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-dup-2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-dup",
                source="live",
                attempt_number=2,
                outbox_id="obox-dup-2",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item2)
        await temp_storage.mark_outbox_queued("obox-dup-2")

        record2 = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-dup-2",
            delivery_plan_id="plan-dup",
            outbox_id="obox-dup-2",
            attempt_number=2,
        )
        await lifecycle.finalize_queued_delivery(
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
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-r1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-nc",
                source="replay",
                replay_run_id="run-10",
                outbox_id="obox-nc",
            ),
        )
        # Live candidate second.
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-l1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-nc",
                source="live",
                outbox_id="obox-nc",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-nc")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-nc",
            delivery_plan_id="plan-nc",
            outbox_id="obox-nc",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-l1"
        assert sent[0].source == "live"

    async def test_multiple_replay_candidates_selects_latest_lineage(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Multiple replay queued receipts for the exact row/attempt →
        the most recent (append-order) replay lineage wins."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-rp1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-rmulti",
                source="replay",
                replay_run_id="run-a",
                outbox_id="obox-rmulti",
                attempt_number=1,
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-rp2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-rmulti",
                source="replay",
                replay_run_id="run-b",
                outbox_id="obox-rmulti",
                attempt_number=1,
            ),
        )
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-rmulti",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-rmulti",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-rmulti")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-rmulti",
            delivery_plan_id="plan-rmulti",
            outbox_id="obox-rmulti",
            attempt_number=1,
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.finalize_queued_delivery(
                temp_storage,
                record=record,
                now=now,
            )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].source == "replay"
        assert sent[0].replay_run_id == "run-b"
        assert sent[0].parent_receipt_id == "rcpt-rp2"
        assert sent[0].adapter_message_id == "pkt-rmulti"
        outbox = await temp_storage.get_outbox_item("obox-rmulti")
        assert outbox is not None
        assert outbox.status == "sent"
        assert "skipping" not in caplog.text
        assert "Hard reject" not in caplog.text

    async def test_single_candidate_no_channel_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """One queued candidate + no channel on record → supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-only",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-single",
                outbox_id="obox-single",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-single")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-single",
            delivery_plan_id="plan-single",
            outbox_id="obox-single",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-first",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=1,
                outbox_id="obox-retry",
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-retry",
                status="queued",
                adapter="m",
                channel="0",
                attempt_number=2,
                outbox_id="obox-retry",
            ),
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-retry",
            delivery_plan_id="plan-001",
            outbox_id="obox-retry",
            attempt_number=2,
        )
        await lifecycle.finalize_queued_delivery(
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
# Replay queued terminal correlation
# ===================================================================


class TestReplayQueuedTerminalCorrelation:
    """Queue-backed replay completions finalize through the real terminal
    callbacks with exact row/attempt correlation.

    A best_effort replay to a queue-backed adapter records a ``queued``
    receipt (SDK enqueue acceptance).  The success (native ref) and
    failure (queue terminal record) callbacks must reach the exact
    replay row/attempt and carry its durable replay lineage; aggregate
    queue-drain state must never manufacture per-attempt delivery truth.
    """

    async def _seed_queued_row(
        self,
        storage: StorageBackend,
        *,
        outbox_id: str,
        attempt_number: int,
        receipt_id: str,
        source: str,
        replay_run_id: str | None,
        event_id: str = "evt-001",
    ) -> None:
        """One queue-backed row: in_progress → queued with its queued receipt.

        All rows share the delivery-plan identity — the production shape
        for live + replay runs of the same event/target (replay atomically
        allocates a fresh row above the maximum effective attempt); only
        outbox_id and attempt_number distinguish them.
        """
        await append_receipt_with_parent(
            storage,
            _make_receipt(
                receipt_id=receipt_id,
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-shared",
                source=source,
                replay_run_id=replay_run_id,
                outbox_id=outbox_id,
                attempt_number=attempt_number,
                event_id=event_id,
            ),
        )
        item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event_id,
            route_id="route-001",
            delivery_plan_id="plan-shared",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=attempt_number,
            dispatch_source=source,  # type: ignore[arg-type]
            replay_run_id=replay_run_id,
        )
        await create_outbox_item_with_parent(
            storage, item, allocate_new_generation=replay_run_id is not None
        )
        await storage.mark_outbox_queued(outbox_id)

    async def test_replay_native_ref_closes_only_its_exact_row(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """The run-B replay callback finalizes only run-B's row with the
        correct source/run/attempt/parent/native-ref lineage; the live row
        and the other replay run's row stay pending, and the terminal row
        is never reclaimable by the retry authority."""
        lifecycle = _make_lifecycle()

        await self._seed_queued_row(
            temp_storage,
            outbox_id="obox-live",
            attempt_number=1,
            receipt_id="rcpt-live-q",
            source="live",
            replay_run_id=None,
        )
        await self._seed_queued_row(
            temp_storage,
            outbox_id="obox-run-a",
            attempt_number=2,
            receipt_id="rcpt-ra-q",
            source="replay",
            replay_run_id="run-a",
        )
        await self._seed_queued_row(
            temp_storage,
            outbox_id="obox-run-b",
            attempt_number=3,
            receipt_id="rcpt-rb-q",
            source="replay",
            replay_run_id="run-b",
        )

        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=OutboundNativeRefRecord(
                event_id="evt-001",
                adapter="m",
                native_channel_id="0",
                native_message_id="pkt-run-b",
                delivery_plan_id="plan-shared",
                outbox_id="obox-run-b",
                attempt_number=3,
            ),
            now=datetime.now(tz=timezone.utc),
        )

        # Run-B's row closed with its own lineage and the real native ref.
        run_b = await temp_storage.get_outbox_item("obox-run-b")
        assert run_b is not None
        assert run_b.status == "sent"
        receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].outbox_id == "obox-run-b"
        assert sent[0].source == "replay"
        assert sent[0].replay_run_id == "run-b"
        assert sent[0].attempt_number == 3
        assert sent[0].parent_receipt_id == "rcpt-rb-q"
        assert sent[0].adapter_message_id == "pkt-run-b"
        refs = await temp_storage.list_native_refs_for_event("evt-001")
        outbound_refs = [r for r in refs if r.direction == "outbound"]
        assert len(outbound_refs) == 1
        assert outbound_refs[0].native_message_id == "pkt-run-b"

        # The live row and the other run's row are untouched: still
        # queued, receipts unchanged.
        live = await temp_storage.get_outbox_item("obox-live")
        assert live is not None
        assert live.status == "queued"
        run_a = await temp_storage.get_outbox_item("obox-run-a")
        assert run_a is not None
        assert run_a.status == "queued"
        assert [r.status for r in receipts if r.outbox_id == "obox-live"] == ["queued"]
        assert [r.status for r in receipts if r.outbox_id == "obox-run-a"] == ["queued"]

        # Past the stale-queued grace, attempts 1 and 2 remain durable
        # queued history but are superseded by generation 3. Recovery must
        # not reclaim an older generation once a newer sibling exists.
        claim_now = (
            datetime.now(tz=timezone.utc)
            + timedelta(seconds=STALE_QUEUED_GRACE_SECONDS + 5)
        ).isoformat()
        claims = await temp_storage.claim_due_outbox_items(
            claim_now,
            worker_id="worker-retry-scan",
        )
        assert claims == []

    async def test_replay_native_failure_stays_failed_rejects_late_success(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """A terminal native failure records the failure with replay
        lineage and can never be upgraded to sent afterwards — not by a
        late or duplicate native-ref callback, and not by a queue drain."""
        lifecycle = _make_lifecycle()
        manager = OutboxManager(temp_storage, lifecycle=lifecycle)

        await self._seed_queued_row(
            temp_storage,
            outbox_id="obox-replay-fail",
            attempt_number=1,
            receipt_id="rcpt-rf-q",
            source="replay",
            replay_run_id="run-9",
        )

        await manager.record_terminal(
            make_terminal_record(
                event_id="evt-001",
                adapter="m",
                outcome="permanent_failed",
                outbox_id="obox-replay-fail",
                delivery_plan_id="plan-shared",
                attempt_number=1,
                native_channel_id="0",
                error="permanent RF encode failure",
                source="replay",
                replay_run_id="run-9",
            )
        )

        row = await temp_storage.get_outbox_item("obox-replay-fail")
        assert row is not None
        assert row.status == "dead_lettered"
        receipts = await temp_storage.list_receipts_for_event("evt-001")
        failed = [r for r in receipts if r.status == "failed"]
        assert len(failed) == 1
        assert failed[0].source == "replay"
        assert failed[0].replay_run_id == "run-9"
        # Failed dispatch evidence remains linked to the queued attempt it
        # terminalizes while preserving replay source attribution.
        assert failed[0].parent_receipt_id == "rcpt-rf-q"
        assert failed[0].failure_kind == "adapter_permanent"
        assert failed[0].attempt_number == 1

        # A late/duplicate success callback for the same row/attempt is
        # stale-rejected: terminal delivery truth was already recorded.
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=OutboundNativeRefRecord(
                event_id="evt-001",
                adapter="m",
                native_channel_id="0",
                native_message_id="pkt-late",
                delivery_plan_id="plan-shared",
                outbox_id="obox-replay-fail",
                attempt_number=1,
            ),
            now=datetime.now(tz=timezone.utc),
        )
        receipts_after = await temp_storage.list_receipts_for_event("evt-001")
        assert [r for r in receipts_after if r.status == "sent"] == []
        row_after = await temp_storage.get_outbox_item("obox-replay-fail")
        assert row_after is not None
        assert row_after.status == "dead_lettered"


# ===================================================================
# Provenance-authoritative queued→sent correlation
# ===================================================================


class TestProvenanceAuthoritativeCorrelation:
    """Provenance-bearing native-ref callbacks validate against durable
    authority before any queued→sent finalization.

    Built-in asynchronous adapters echo the immutable attempt envelope frozen
    before hand-off.  Core rejects callbacks whose envelope contradicts the
    durable outbox row or already-present queued receipt evidence, and
    finalizes with the envelope's lineage when everything agrees.
    """

    def _provenance(self, **overrides: object) -> DeliveryAttemptProvenance:
        values: dict[str, object] = {
            "event_id": "evt-001",
            "delivery_plan_id": "plan-prov",
            "target_adapter": "m",
            "target_channel": "0",
            "outbox_id": "obox-prov",
            "attempt_number": 1,
            "source": "replay",
            "replay_run_id": "run-b",
        }
        values.update(overrides)
        return DeliveryAttemptProvenance(**values)  # type: ignore[arg-type]

    async def _seed_replay_row(
        self,
        storage: StorageBackend,
        *,
        outbox_id: str = "obox-prov",
    ) -> None:
        """One queued replay generation (attempt 1) for the shared identity."""
        item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-prov",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
            dispatch_source="replay",
            replay_run_id="run-b",
        )
        await create_outbox_item_with_parent(
            storage, item, allocate_new_generation=True
        )
        await storage.mark_outbox_queued(outbox_id)

    def _record(self, provenance: DeliveryAttemptProvenance) -> OutboundNativeRefRecord:
        return OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-prov",
            delivery_plan_id="plan-prov",
            outbox_id=provenance.outbox_id,
            attempt_provenance=provenance,
        )

    async def test_provenance_callback_rejects_row_contradiction(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """A live-sourced envelope for a replay row is rejected before any
        receipt correlation — no supplemental receipt, row untouched."""
        await self._seed_replay_row(temp_storage)
        lifecycle = _make_lifecycle()

        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=self._record(
                self._provenance(
                    outbox_id="obox-prov", source="live", replay_run_id=None
                )
            ),
            now=datetime.now(tz=timezone.utc),
        )

        receipts = await temp_storage.list_receipts_for_event("evt-001")
        assert receipts == []
        row = await temp_storage.get_outbox_item("obox-prov")
        assert row is not None
        assert row.status == "queued"

    async def test_provenance_callback_rejects_contradictory_queued_receipt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Queued evidence whose lineage disagrees with the envelope rejects
        the callback instead of vanishing through pre-filtering."""
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-prov-mismatch",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-prov",
                source="live",
                outbox_id="obox-prov",
                attempt_number=1,
            ),
        )
        await self._seed_replay_row(temp_storage)
        lifecycle = _make_lifecycle()

        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=self._record(self._provenance(outbox_id="obox-prov")),
            now=datetime.now(tz=timezone.utc),
        )

        receipts = await temp_storage.list_receipts_for_event("evt-001")
        assert [r.status for r in receipts] == ["queued"]
        assert receipts[0].source == "live"
        row = await temp_storage.get_outbox_item("obox-prov")
        assert row is not None
        assert row.status == "queued"

    async def test_provenance_callback_rejects_corrupt_receipt_identity(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Outbox-scoped receipt reads must expose malformed plan identity."""
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-prov-wrong-plan",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="wrong-plan",
                source="replay",
                replay_run_id="run-b",
                outbox_id="obox-prov",
                attempt_number=1,
            ),
        )
        await self._seed_replay_row(temp_storage)
        lifecycle = _make_lifecycle()

        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=self._record(self._provenance(outbox_id="obox-prov")),
            now=datetime.now(tz=timezone.utc),
        )

        receipts = await temp_storage.list_receipts_for_event("evt-001")
        assert [r.receipt_id for r in receipts] == ["rcpt-prov-wrong-plan"]
        row = await temp_storage.get_outbox_item("obox-prov")
        assert row is not None
        assert row.status == "queued"

    async def test_provenance_callback_finalizes_with_envelope_lineage(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Agreeing queued evidence finalizes from the envelope: the last
        matching receipt supplies parent linkage, the envelope supplies
        source/run lineage, and the row closes at the exact generation."""
        for receipt_id in ("rcpt-prov-a", "rcpt-prov-b"):
            await append_receipt_with_parent(
                temp_storage,
                _make_receipt(
                    receipt_id=receipt_id,
                    status="queued",
                    adapter="m",
                    channel="0",
                    plan_id="plan-prov",
                    source="replay",
                    replay_run_id="run-b",
                    outbox_id="obox-prov",
                    attempt_number=1,
                ),
            )
        await self._seed_replay_row(temp_storage)
        lifecycle = _make_lifecycle()

        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=self._record(self._provenance(outbox_id="obox-prov")),
            now=datetime.now(tz=timezone.utc),
        )

        receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].source == "replay"
        assert sent[0].replay_run_id == "run-b"
        assert sent[0].parent_receipt_id == "rcpt-prov-b"
        assert sent[0].attempt_number == 1
        assert sent[0].outbox_id == "obox-prov"
        assert sent[0].adapter_message_id == "pkt-prov"
        row = await temp_storage.get_outbox_item("obox-prov")
        assert row is not None
        assert row.status == "sent"
