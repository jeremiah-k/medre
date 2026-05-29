"""Tests for supplemental queued→sent receipt generation.

Exercises ``append_queued_to_sent_receipt`` including happy paths,
outbox transitions, error handling, and deterministic plan_id
correlation (Tranche 5 regression).
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
# Same-channel retry lineage regression (Tranche 6)
# ===================================================================


class TestSameChannelRetryLineageRegression:
    """Regression tests for same-channel retry lineage when
    native_channel_id is missing.

    These tests verify that ``append_queued_to_sent_receipt`` correctly
    resolves unambiguous same-channel retry lineages and correctly
    rejects cross-channel ambiguity, for both the deterministic plan_id
    path and the legacy degraded path.
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

    async def test_legacy_no_plan_no_channel_same_plan_same_channel_multiple_attempts(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """(C) legacy no plan + no native_channel_id + same plan + same
        channel + multiple attempts → supplemental sent receipt, parent
        latest queued attempt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-c1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-c",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-c3",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-c",
                attempt_number=3,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-c",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-c3"
        assert sent[0].attempt_number == 3

    async def test_legacy_no_plan_no_channel_same_plan_multiple_channels_skip(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(D) legacy no plan + no native_channel_id + same plan + multiple
        channels → no supplemental receipt, warning logged."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-d0",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-d",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-d1",
                status="queued",
                adapter="m",
                channel="1",
                plan_id="plan-d",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-d",
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
        assert "Ambiguous legacy queued receipt correlation" in caplog.text

    async def test_legacy_no_plan_no_channel_multiple_plan_ids_skip(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """(E) legacy no plan + no native_channel_id + multiple plan IDs →
        no supplemental receipt, warning logged."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-e1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-e1",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-e2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-e2",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-e",
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
        assert "Ambiguous legacy queued receipt correlation" in caplog.text

    async def test_single_candidate_no_channel_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """One queued candidate + no channel on record → supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-only", status="queued", adapter="m", channel="0"
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-single",
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
            )
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=datetime.now(timezone.utc),
            )
        assert "Failed to transition outbox queued->sent" in caplog.text


# ===================================================================
# Deterministic delivery_plan_id correlation (Tranche 5 regression)
# ===================================================================


class TestDeterministicPlanIdCorrelation:
    """Regression tests for delivery_plan_id-based queued→sent correlation.

    These tests verify that ``append_queued_to_sent_receipt`` uses the
    ``delivery_plan_id`` on ``OutboundNativeRefRecord`` for deterministic
    correlation, falling back to the legacy heuristic only when
    ``delivery_plan_id`` is absent.
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

    async def test_missing_plan_id_legacy_heuristic_applies(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No delivery_plan_id, multiple candidates with DIFFERENT plan_ids on
        same channel → cross-plan ambiguity → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        # Two queued receipts with DIFFERENT plan_ids on same channel.
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-p1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-p1",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-p2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-p2",
            )
        )

        # Record WITHOUT delivery_plan_id — legacy degraded path applies.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-ambig",
            # delivery_plan_id is None (not set)
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        # Cross-plan ambiguity: no supplemental receipt created.
        assert len(sent) == 0

    async def test_missing_plan_id_single_candidate_succeeds(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No delivery_plan_id, single candidate → legacy path succeeds."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-solo",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-solo",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-solo",
            # delivery_plan_id is None — legacy path
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].delivery_plan_id == "plan-solo"

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

    async def test_legacy_same_plan_different_attempts_latest_wins(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No delivery_plan_id + same plan_id, different attempt_numbers
        → latest attempt wins (retry lineage)."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-att1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry-legacy",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-att3",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-retry-legacy",
                attempt_number=3,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-legacy-retry",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-att3"
        assert sent[0].attempt_number == 3

    async def test_legacy_no_plan_no_channel_empty_plan_id_uniform_latest_wins(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """(F) legacy no plan + no channel + multiple candidates all with
        delivery_plan_id="" + same target_channel → {""} plan uniformity
        → supplemental sent receipt, parent latest queued attempt.

        Regression: confirms that the empty-string plan_id set {""}
        satisfies the uniformity check (len == 1) just like any other
        single-value plan set.
        """
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-f1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="",
                attempt_number=1,
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-f2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="",
                attempt_number=2,
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-f",
        )
        await lifecycle.append_queued_to_sent_receipt(
            temp_storage,
            record=record,
            now=now,
        )

        all_receipts = await temp_storage.list_receipts_for_event("evt-001")
        sent = [r for r in all_receipts if r.status == "sent"]
        assert len(sent) == 1
        assert sent[0].parent_receipt_id == "rcpt-f2"
        assert sent[0].attempt_number == 2
        assert sent[0].delivery_plan_id == ""

    async def test_legacy_cross_plan_no_channel_skips(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """No delivery_plan_id + no channel + candidates with different
        plan_ids → no supplemental receipt."""
        lifecycle = _make_lifecycle()
        now = datetime.now(tz=timezone.utc)

        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-x1",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-x1",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-x2",
                status="queued",
                adapter="m",
                channel="1",
                plan_id="plan-x2",
            )
        )

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-cross",
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
