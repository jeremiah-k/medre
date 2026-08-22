"""Error-path tests for atomic queued-delivery finalization.

Split from ``test_lifecycle_queued_to_sent.py`` so each lifecycle test module
stays below the repository's 1,500-line hard ceiling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from medre.core.contracts.adapter import OutboundNativeRefRecord
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

from .conftest import _make_lifecycle, _make_receipt
from tests.helpers.storage_outbox import (
    admit_event,
    append_receipt_with_parent,
    create_outbox_item_with_parent,
)

# ===================================================================
# finalize_queued_delivery — error paths
# ===================================================================


class TestAppendQueuedToSentErrorPaths:
    """Error paths in finalize_queued_delivery."""

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
            await lifecycle.finalize_queued_delivery(
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
        await append_receipt_with_parent(
            temp_storage,
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
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=datetime.now(timezone.utc),
        )
        all_r = await temp_storage.list_receipts_for_event("evt-001")
        assert all(r.status != "sent" for r in all_r)

    async def test_lost_outbox_guard_logged_not_raised(
        self,
        temp_storage: StorageBackend,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Finalization losing its outbox guard logs a warning and does
        not raise — no evidence is committed for a stale attempt."""
        caplog.set_level(logging.DEBUG)
        from unittest.mock import AsyncMock, patch

        lifecycle = _make_lifecycle()
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-ob",
                status="queued",
                adapter="m",
                channel="0",
                outbox_id="obox-supp-err",
            ),
        )
        # Matching outbox row so exact correlation reaches finalization.
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
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
            "finalize_queued_delivery",
            AsyncMock(return_value=False),
        ):
            # Must not raise even though the atomic transaction reported
            # that the guarded attempt was no longer finalizable.
            await lifecycle.finalize_queued_delivery(
                temp_storage,
                record=record,
                now=datetime.now(timezone.utc),
            )
        assert "lost its outbox guard" in caplog.text

    async def test_finalization_storage_error_propagates(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """A storage failure inside the atomic finalization propagates to
        the caller instead of being swallowed as success."""
        from unittest.mock import AsyncMock, patch

        from medre.core.storage.backend import StorageError

        lifecycle = _make_lifecycle()
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-ob",
                status="queued",
                adapter="m",
                channel="0",
                outbox_id="obox-supp-err",
            ),
        )
        # Matching outbox row so exact correlation reaches finalization.
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
        await create_outbox_item_with_parent(temp_storage, outbox_item)
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
            "finalize_queued_delivery",
            AsyncMock(side_effect=StorageError("finalization txn failed")),
        ):
            with pytest.raises(StorageError, match="finalization txn failed"):
                await lifecycle.finalize_queued_delivery(
                    temp_storage,
                    record=record,
                    now=datetime.now(timezone.utc),
                )

    async def test_attempt_filter_selects_matching_attempt_receipt(
        self,
        temp_storage: StorageBackend,
    ) -> None:
        """Queued receipts sharing an outbox_id across attempts must be
        filtered by attempt_number before finalization."""
        lifecycle = _make_lifecycle()
        await admit_event(temp_storage, "evt-attempt-filter")

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-attempt-filter",
            event_id="evt-attempt-filter",
            route_id="route-af",
            delivery_plan_id="plan-af",
            target_adapter="m",
            target_channel="0",
            status="in_progress",
            attempt_number=2,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)
        await temp_storage.mark_outbox_queued("obox-attempt-filter")

        # Two queued receipts share the outbox_id but belong to different
        # attempts; only attempt 2 may be finalized for this callback.
        # Attempt 2 is appended FIRST so that unfiltered "latest wins"
        # preference would pick attempt 1 — proving the explicit
        # attempt_number filter is what selects the right receipt.
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-af-a2",
                status="queued",
                adapter="m",
                channel="0",
                event_id="evt-attempt-filter",
                outbox_id="obox-attempt-filter",
                attempt_number=2,
            ),
        )
        await append_receipt_with_parent(
            temp_storage,
            _make_receipt(
                receipt_id="rcpt-af-a1",
                status="queued",
                adapter="m",
                channel="0",
                event_id="evt-attempt-filter",
                outbox_id="obox-attempt-filter",
                attempt_number=1,
            ),
        )

        record = OutboundNativeRefRecord(
            event_id="evt-attempt-filter",
            adapter="m",
            native_channel_id="0",
            native_message_id="pkt-af-2",
            delivery_plan_id="plan-af",
            outbox_id="obox-attempt-filter",
            attempt_number=2,
        )
        await lifecycle.finalize_queued_delivery(
            temp_storage,
            record=record,
            now=datetime.now(timezone.utc),
        )

        receipts = {
            r.receipt_id: r
            for r in await temp_storage.list_receipts_for_event("evt-attempt-filter")
        }
        # Attempt 1's queued receipt is untouched.
        assert receipts["rcpt-af-a1"].status == "queued"
        # Attempt 2 gained a sent supplemental receipt tied to this callback.
        sent = [
            r
            for r in receipts.values()
            if r.status == "sent"
            and r.attempt_number == 2
            and r.adapter_message_id == "pkt-af-2"
        ]
        assert len(sent) == 1
        assert sent[0].outbox_id == "obox-attempt-filter"
