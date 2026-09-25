"""Tests for supplemental queued→sent receipt generation.

Exercises ``finalize_queued_delivery`` including happy paths,
outbox transitions, error handling, outbox_id-based correlation,
retry lineage, and delivery state guards.

Source-aware candidate selection tests live in
``test_lifecycle_queued_to_sent_source_selection.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from tests.helpers.delivery_callbacks import make_attempt_provenance
from tests.helpers.storage_outbox import (
    append_receipt_with_parent,
    create_outbox_item_with_parent,
)

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
            outbox_id="obox-supplemental-sent",
        )
        await append_receipt_with_parent(temp_storage, queued)

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-supplemental-sent",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-q",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-supplemental-sent")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="mesh-1",
                outbox_id="obox-supplemental-sent",
                attempt_number=1,
                delivery_plan_id="plan-q",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-42",
            delivery_plan_id="plan-q",
            outbox_id="obox-supplemental-sent",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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
# Same-channel retry lineage regression (outbox_id-based correlation)
# ===================================================================


class TestSameChannelRetryLineageRegression:
    """Regression tests for same-channel retry lineage when
    native_channel_id is missing.

    These tests verify that ``finalize_queued_delivery`` correctly
    resolves unambiguous same-channel retry lineages and correctly
    rejects cross-channel ambiguity for the exact outbox_id + attempt_number path.
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
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-a1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-r",
                attempt_number=1,
                outbox_id="obox-retry-multi",
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-a2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-r",
                attempt_number=2,
                outbox_id="obox-retry-multi",
            ),
        )

        # Create matching outbox item for exact correlation (attempt 2).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-retry-multi",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-r",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry-multi")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-retry-multi",
                attempt_number=2,
                delivery_plan_id="plan-r",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-a",
            delivery_plan_id="plan-r",
            outbox_id="obox-retry-multi",
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
        assert sent[0].parent_receipt_id == "rcpt-a2"
        assert sent[0].attempt_number == 2
        assert await temp_storage.resolve_native_ref("m", "0", "pkt-a") == "evt-001"
        assert await temp_storage.resolve_native_ref("m", None, "pkt-a") is None


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
            outbox_id="obox-supplemental",
        )
        await append_receipt_with_parent(temp_storage, queued)

        # Create a matching outbox item, then transition to "queued" (Pattern C).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-supplemental",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-outbox",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-supplemental")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="mesh-1",
                outbox_id="obox-supplemental",
                attempt_number=1,
                delivery_plan_id="plan-outbox",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-outbox-42",
            delivery_plan_id="plan-outbox",
            outbox_id="obox-supplemental",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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
# outbox_id-based correlation regression
# ===================================================================


class TestDeterministicPlanIdCorrelation:
    """Regression tests for outbox_id-based queued→sent correlation.

    These tests verify that ``finalize_queued_delivery`` uses
    ``outbox_id`` for exact receipt selection, with ``delivery_plan_id``
    serving as a validation field.  When ``outbox_id`` is absent, the
    callback is hard-rejected and no supplemental receipt is created.
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
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-plan-a",
                status="queued",
                adapter="mesh",
                channel="0",
                plan_id="plan-a",
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-plan-b",
                status="queued",
                adapter="mesh",
                channel="0",
                plan_id="plan-b",
                outbox_id="obox-plan-b",
            ),
        )

        # Create matching outbox item for plan-b.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-plan-b",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-b",
            target_adapter="mesh",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-plan-b")

        # Record for plan-b with delivery_plan_id set.
        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="mesh",
                outbox_id="obox-plan-b",
                attempt_number=1,
                delivery_plan_id="plan-b",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="mesh",
            native_channel_id="0",
            native_message_id="pkt-plan-b",
            delivery_plan_id="plan-b",
            outbox_id="obox-plan-b",
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

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-a2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-a2",
                outbox_id="obox-a2",
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-b2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-b2",
                outbox_id="obox-b2",
            ),
        )

        # Create matching outbox items for both plans.
        outbox_a = DeliveryOutboxItem(
            outbox_id="obox-a2",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-a2",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_a)
        await temp_storage.mark_outbox_queued("obox-a2")

        outbox_b = DeliveryOutboxItem(
            outbox_id="obox-b2",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-b2",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_b)
        await temp_storage.mark_outbox_queued("obox-b2")

        # Record for plan-a2.
        record_a = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-a2",
                attempt_number=1,
                delivery_plan_id="plan-a2",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-a2",
            delivery_plan_id="plan-a2",
            outbox_id="obox-a2",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record_a,
            now=now,
        )

        # Record for plan-b2.
        record_b = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-b2",
                attempt_number=1,
                delivery_plan_id="plan-b2",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-b2",
            delivery_plan_id="plan-b2",
            outbox_id="obox-b2",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
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
        """Same outbox, multiple queued attempts → exact outbox_id match."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-attempt1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry",
                attempt_number=1,
                outbox_id="obox-retry-latest",
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-attempt2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry",
                attempt_number=2,
                outbox_id="obox-retry-latest",
            ),
        )

        # Create matching outbox item for exact correlation (attempt 2).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-retry-latest",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-retry",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry-latest")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-retry-latest",
                attempt_number=2,
                delivery_plan_id="plan-retry",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-latest",
            delivery_plan_id="plan-retry",
            outbox_id="obox-retry-latest",
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
        assert sent[0].parent_receipt_id == "rcpt-attempt2"
        assert sent[0].attempt_number == 2

    async def test_delivery_plan_id_channel_match_still_works(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Record has plan_id + channel, matching receipt exists → sent receipt
        created correctly."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-match",
                status="queued",
                adapter="mesh-1",
                channel="0",
                plan_id="plan-match",
                outbox_id="obox-match",
            ),
        )

        # Create matching outbox item for exact correlation.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-match",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-match",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-match")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="mesh-1",
                outbox_id="obox-match",
                attempt_number=1,
                delivery_plan_id="plan-match",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-match-ok",
            delivery_plan_id="plan-match",
            outbox_id="obox-match",
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
        assert sent[0].parent_receipt_id == "rcpt-match"
        assert sent[0].delivery_plan_id == "plan-match"
        assert sent[0].adapter_message_id == "pkt-match-ok"


# ===================================================================
# delivery_state transition guard at queued→sent (D)
# ===================================================================


class TestDeliveryStateTransitionGuard:
    """Verify that finalize_queued_delivery validates the selected
    queued receipt can transition to sent via delivery_state helper."""


# ===================================================================
# Supplemental queued→sent receipt — uncovered edge-case paths
# ===================================================================


class TestAppendQueuedToSentEdgeCases:
    """Edge-case rejection paths in finalize_queued_delivery."""

    async def test_no_queued_receipt_matched_outbox_id(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Valid outbox item exists but no queued receipt with matching
        outbox_id → early return with debug log.  Covers lines 744-755.
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Queued receipt with a DIFFERENT outbox_id (won't match).
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-other-oid",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-nom",
                outbox_id="obox-different",
            ),
        )

        # Outbox item matching the record's outbox_id.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-nom",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-nom",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-nom")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-nom",
                attempt_number=1,
                delivery_plan_id="plan-nom",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-nom",
            delivery_plan_id="plan-nom",
            outbox_id="obox-nom",
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
        assert len(sent) == 0
        assert "No queued receipt matched outbox_id" in caplog.text

    async def test_attempt_number_mismatch_outbox_vs_receipt(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Outbox item attempt_number != queued receipt attempt_number →
        stale callback rejection.  Covers lines 768-769 and 781.
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Queued receipt with attempt_number=1 (stale).
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-stale-attempt",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-atm",
                attempt_number=1,
                outbox_id="obox-atm",
            ),
        )

        # Outbox item now on attempt_number=2 (retry happened).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-atm",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-atm",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-atm")

        # Record with attempt_number=2 matches outbox but mismatches receipt.
        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="m",
                outbox_id="obox-atm",
                attempt_number=2,
                delivery_plan_id="plan-atm",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-atm",
            delivery_plan_id="plan-atm",
            outbox_id="obox-atm",
            attempt_number=2,
        )
        with caplog.at_level(logging.WARNING):
            await lifecycle.finalize_queued_delivery(
                temp_storage,
                record=record,
                now=now,
            )

        # Outcome-focused: the stale callback must not commit any sent
        # evidence, regardless of whether the attempt filter (pre-selection)
        # or the downstream mismatch guard performed the rejection.
        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 0
        assert not [r for r in all_receipts if r.adapter_message_id == "pkt-atm"]

    async def test_mark_outbox_sent_happy_path_validated_outbox(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """validated_outbox is not None → mark_outbox_sent is called and
        transitions the outbox item.  Covers lines 842-849 happy path.

        This is essentially the same as test_outbox_transitioned_from_queued_to_sent
        but explicitly exercises the validated_outbox path.
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        queued = _make_receipt(
            receipt_id="rcpt-vo",
            status="queued",
            adapter="mesh-1",
            channel="0",
            plan_id="plan-vo",
            outbox_id="obox-vo",
        )
        await append_receipt_with_parent(temp_storage, queued)

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-vo",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-vo",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-vo")

        record = OutboundNativeRefRecord(
            attempt_provenance=make_attempt_provenance(
                event_id="evt-001",
                target_adapter="mesh-1",
                outbox_id="obox-vo",
                attempt_number=1,
                delivery_plan_id="plan-vo",
                target_channel="0",
            ),
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-vo",
            delivery_plan_id="plan-vo",
            outbox_id="obox-vo",
            attempt_number=1,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=now,
        )

        updated = await temp_storage.get_outbox_item("obox-vo")
        assert updated is not None
        assert updated.status == "sent"

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].outbox_id == "obox-vo"


class TestCallbackRequiresAttemptProvenance:
    """Asynchronous callback records cannot exist without the envelope."""

    def test_outboxless_record_shape_is_unconstructible(self) -> None:
        """The pre-envelope plan-id-only callback shape no longer builds."""
        with pytest.raises(ValueError, match="requires attempt_provenance"):
            OutboundNativeRefRecord(
                event_id="evt-001",
                adapter="m",
                native_channel_id="0",
                native_message_id="pkt-nope",
                delivery_plan_id="plan-x",
            )

    def test_envelope_requires_exact_attempt_identity(self) -> None:
        """An envelope without an outbox cannot describe a durable attempt."""
        from medre.core.events import DeliveryAttemptProvenance

        with pytest.raises(ValueError, match="non-empty"):
            DeliveryAttemptProvenance(
                event_id="evt-001",
                delivery_plan_id="plan-x",
                target_adapter="m",
                target_channel="0",
                outbox_id="",
                attempt_number=1,
                source="live",
            )
