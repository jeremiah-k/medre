"""Tests for supplemental queued→sent receipt generation.

Exercises ``append_queued_to_sent_receipt`` including happy paths,
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
        await temp_storage.append_receipt(queued)

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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-supplemental-sent")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-42",
            delivery_plan_id="plan-q",
            outbox_id="obox-supplemental-sent",
            attempt_number=1,
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
                outbox_id="obox-retry-multi",
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
                outbox_id="obox-retry-multi",
            )
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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry-multi")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id=None,
            native_message_id="pkt-a",
            delivery_plan_id="plan-r",
            outbox_id="obox-retry-multi",
            attempt_number=2,
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
        assert "Hard reject" in caplog.text
        assert "lacks outbox_id" in caplog.text
        assert "plan-bx" in caplog.text


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
        await temp_storage.append_receipt(queued)

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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-supplemental")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="packet-outbox-42",
            delivery_plan_id="plan-outbox",
            outbox_id="obox-supplemental",
            attempt_number=1,
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
                receipt_id="rcpt-ob",
                status="queued",
                adapter="m",
                channel="0",
                outbox_id="obox-supp-err",
            )
        )

        # Create a matching outbox item in storage so exact correlation works.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-supp-err",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-001",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=1,
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-supp-err")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt",
            delivery_plan_id="plan-001",
            outbox_id="obox-supp-err",
            attempt_number=1,
        )
        with patch.object(
            temp_storage,
            "mark_outbox_sent",
            AsyncMock(side_effect=RuntimeError("outbox write fail")),
        ):
            await lifecycle.append_queued_to_sent_receipt(
                temp_storage,
                record=record,
                now=datetime.now(timezone.utc),
            )


# ===================================================================
# outbox_id-based correlation regression
# ===================================================================


class TestDeterministicPlanIdCorrelation:
    """Regression tests for outbox_id-based queued→sent correlation.

    These tests verify that ``append_queued_to_sent_receipt`` uses
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
                outbox_id="obox-plan-b",
            )
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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-plan-b")

        # Record for plan-b with delivery_plan_id set.
        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh",
            native_channel_id="0",
            native_message_id="pkt-plan-b",
            delivery_plan_id="plan-b",
            outbox_id="obox-plan-b",
            attempt_number=1,
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
                outbox_id="obox-a2",
            )
        )
        await temp_storage.append_receipt(
            _make_receipt(
                receipt_id="rcpt-b2",
                status="queued",
                adapter="m",
                channel="0",
                plan_id="plan-b2",
                outbox_id="obox-b2",
            )
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
        await temp_storage.create_outbox_item(outbox_a)
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
        await temp_storage.create_outbox_item(outbox_b)
        await temp_storage.mark_outbox_queued("obox-b2")

        # Record for plan-a2.
        record_a = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-a2",
            delivery_plan_id="plan-a2",
            outbox_id="obox-a2",
            attempt_number=1,
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
            outbox_id="obox-b2",
            attempt_number=1,
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
                outbox_id="obox-retry-latest",
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
                outbox_id="obox-retry-latest",
            )
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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-retry-latest")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-latest",
            delivery_plan_id="plan-retry",
            outbox_id="obox-retry-latest",
            attempt_number=2,
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
            if "Hard reject" in r.message and "lacks outbox_id" in r.message
        ]
        assert len(warning_records) >= 1
        assert warning_records[0].levelname == "WARNING"
        # Must include operator context.
        assert "evt-warn" in caplog.text
        assert "mesh-1" in caplog.text
        assert "ch-warn-42" in caplog.text

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

        # Create a matching outbox item, then transition to "queued" (Pattern C).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-no-plan",
            event_id="evt-001",
            route_id="route-001",
            delivery_plan_id="plan-ob",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-no-plan")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-no-plan-ob",
            attempt_number=1,
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
                outbox_id="obox-match",
            )
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
        await temp_storage.create_outbox_item(outbox_item)
        await temp_storage.mark_outbox_queued("obox-match")

        record = OutboundNativeRefRecord(
            event_id="evt-001",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="pkt-match-ok",
            delivery_plan_id="plan-match",
            outbox_id="obox-match",
            attempt_number=1,
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
