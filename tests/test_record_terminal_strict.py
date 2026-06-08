"""Strict validation tests for OutboxManager.record_terminal.

Verifies that record_terminal enforces exact outbox correlation by
rejecting terminal outcome records that:
- Lack outbox_id
- Point to non-existent outbox rows
- Reference already-terminal outbox rows
- Have mismatched event_id, adapter, channel, plan, or attempt_number
- Target outbox rows in ineligible statuses (pending, retry_wait)

Also validates that valid callbacks produce correct receipts and outbox
transitions, and that route_id is preserved from the outbox row.
"""

from __future__ import annotations

import logging

import pytest

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage

# -- Helpers --


def _make_manager(storage: SQLiteStorage) -> OutboxManager:
    return OutboxManager(
        storage=storage,
        lifecycle=DeliveryLifecycleService(),
    )


async def _create_outbox_item(
    storage: SQLiteStorage,
    *,
    outbox_id: str = "obox-test",
    event_id: str = "evt-test",
    route_id: str = "route-1",
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "mesh-1",
    target_channel: str | None = "0",
    attempt_number: int = 1,
    status: str = "in_progress",
) -> DeliveryOutboxItem:
    """Create and persist an outbox item, optionally transitioning status."""
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
    )
    await storage.create_outbox_item(item)
    return item


# ===================================================================
# 1. No outbox_id → hard reject
# ===================================================================


class TestNoOutboxIdRejected:
    """outbox_id=None must be rejected — no receipt, no mutation."""

    @pytest.mark.asyncio
    async def test_no_outbox_id_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-no-obox",
            adapter="mesh-1",
            outbox_id=None,
            outcome="exhausted",
            error="budget exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-no-obox")
        assert len(receipts) == 0
        assert "no outbox_id" in caplog.text


# ===================================================================
# 2. Missing outbox row → reject
# ===================================================================


class TestMissingOutboxRowRejected:
    """outbox_id pointing to a non-existent row → reject."""

    @pytest.mark.asyncio
    async def test_missing_outbox_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-missing",
            adapter="mesh-1",
            outbox_id="obox-nonexistent",
            outcome="exhausted",
            error="budget exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-missing")
        assert len(receipts) == 0
        assert "not found" in caplog.text


# ===================================================================
# 3. Terminal outbox status → reject
# ===================================================================


class TestTerminalOutboxStatusRejected:
    """Already-terminal outbox rows must be rejected."""

    @pytest.mark.asyncio
    async def test_sent_row_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """outbox status is 'sent' → no receipt."""
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-sent",
            event_id="evt-sent",
            status="in_progress",
        )
        # Transition: in_progress -> queued -> sent
        await temp_storage.mark_outbox_queued("obox-sent")
        await temp_storage.mark_outbox_sent("obox-sent")

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-sent",
            adapter="mesh-1",
            outbox_id="obox-sent",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-sent")
        assert len(receipts) == 0
        assert "already terminal" in caplog.text


# ===================================================================
# 4. retry_wait status → reject
# ===================================================================


class TestRetryWaitStatusRejected:
    """retry_wait is not eligible for terminal outcomes."""

    @pytest.mark.asyncio
    async def test_retry_wait_row_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-rw",
            event_id="evt-rw",
            status="in_progress",
        )
        await temp_storage.mark_outbox_retry_wait(
            "obox-rw",
            next_attempt_at="2099-01-01T00:00:00+00:00",
            failure_kind="adapter_transient",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-rw",
            adapter="mesh-1",
            outbox_id="obox-rw",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-rw")
        assert len(receipts) == 0
        assert "not eligible" in caplog.text


# ===================================================================
# 5. pending status → reject
# ===================================================================


class TestPendingStatusRejected:
    """pending is not eligible for terminal outcomes."""

    @pytest.mark.asyncio
    async def test_pending_row_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-pending",
            event_id="evt-pending",
            status="pending",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-pending",
            adapter="mesh-1",
            outbox_id="obox-pending",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-pending")
        assert len(receipts) == 0
        assert "not eligible" in caplog.text


# ===================================================================
# 6. Wrong event_id → reject
# ===================================================================


class TestWrongEventIdRejected:
    """event_id mismatch between record and outbox row → reject."""

    @pytest.mark.asyncio
    async def test_wrong_event_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-evt-mismatch",
            event_id="evt-correct",
            target_adapter="mesh-1",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-wrong",
            adapter="mesh-1",
            outbox_id="obox-evt-mismatch",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts_correct = await temp_storage.list_receipts_for_event("evt-correct")
        receipts_wrong = await temp_storage.list_receipts_for_event("evt-wrong")
        assert len(receipts_correct) == 0
        assert len(receipts_wrong) == 0
        assert "event_id" in caplog.text


# ===================================================================
# 7. Wrong adapter → reject
# ===================================================================


class TestWrongAdapterRejected:
    """adapter mismatch between record and outbox row → reject."""

    @pytest.mark.asyncio
    async def test_wrong_adapter_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-adapter-mismatch",
            event_id="evt-adapter",
            target_adapter="mesh-correct",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-adapter",
            adapter="mesh-wrong",
            outbox_id="obox-adapter-mismatch",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-adapter")
        assert len(receipts) == 0
        assert "target_adapter" in caplog.text


# ===================================================================
# 8. Wrong channel → reject
# ===================================================================


class TestWrongChannelRejected:
    """native_channel_id mismatch between record and outbox row → reject."""

    @pytest.mark.asyncio
    async def test_wrong_channel_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-ch-mismatch",
            event_id="evt-channel",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-channel",
            adapter="mesh-1",
            native_channel_id="99",
            outbox_id="obox-ch-mismatch",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-channel")
        assert len(receipts) == 0
        assert "target_channel" in caplog.text


# ===================================================================
# 9. Wrong plan → reject
# ===================================================================


class TestWrongPlanRejected:
    """delivery_plan_id mismatch between record and outbox row → reject."""

    @pytest.mark.asyncio
    async def test_wrong_plan_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-plan-mismatch",
            event_id="evt-plan",
            target_adapter="mesh-1",
            delivery_plan_id="plan-correct",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-plan",
            adapter="mesh-1",
            outbox_id="obox-plan-mismatch",
            delivery_plan_id="plan-wrong",
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-plan")
        assert len(receipts) == 0
        assert "delivery_plan_id" in caplog.text


# ===================================================================
# 10. Wrong attempt_number → reject
# ===================================================================


class TestWrongAttemptNumberRejected:
    """attempt_number mismatch between record and outbox row → reject."""

    @pytest.mark.asyncio
    async def test_wrong_attempt_number_no_receipt(
        self,
        temp_storage: SQLiteStorage,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-attempt-mismatch",
            event_id="evt-attempt",
            target_adapter="mesh-1",
            attempt_number=3,
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-attempt",
            adapter="mesh-1",
            outbox_id="obox-attempt-mismatch",
            attempt_number=1,
            outcome="exhausted",
        )
        with caplog.at_level(logging.WARNING):
            await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-attempt")
        assert len(receipts) == 0
        assert "attempt_number" in caplog.text


# ===================================================================
# 11. Valid exhausted → receipt + dead_lettered
# ===================================================================


class TestValidExhausted:
    """Correct exhausted callback produces 1 failed receipt and dead_lettered outbox."""

    @pytest.mark.asyncio
    async def test_valid_exhausted(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-exhausted",
            event_id="evt-exhausted",
            target_adapter="mesh-1",
            delivery_plan_id="plan-ex",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-exhausted",
            adapter="mesh-1",
            outbox_id="obox-exhausted",
            delivery_plan_id="plan-ex",
            outcome="exhausted",
            error="budget exhausted",
        )
        await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-exhausted")
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        assert receipts[0].failure_kind == "adapter_transient"
        assert receipts[0].outbox_id == "obox-exhausted"

        outbox = await temp_storage.get_outbox_item("obox-exhausted")
        assert outbox is not None
        assert outbox.status == "dead_lettered"


# ===================================================================
# 12. Valid permanent_failed → receipt + dead_lettered
# ===================================================================


class TestValidPermanentFailed:
    """Correct permanent_failed callback produces receipt and dead_lettered outbox."""

    @pytest.mark.asyncio
    async def test_valid_permanent_failed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-perm",
            event_id="evt-perm",
            target_adapter="mesh-1",
            delivery_plan_id="plan-perm",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-perm",
            adapter="mesh-1",
            outbox_id="obox-perm",
            delivery_plan_id="plan-perm",
            outcome="permanent_failed",
            error="permanent failure",
        )
        await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-perm")
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        assert receipts[0].failure_kind == "adapter_permanent"

        outbox = await temp_storage.get_outbox_item("obox-perm")
        assert outbox is not None
        assert outbox.status == "dead_lettered"


# ===================================================================
# 13. Valid cancelled → receipt + cancelled outbox
# ===================================================================


class TestValidCancelled:
    """Correct cancelled callback produces receipt and cancelled outbox."""

    @pytest.mark.asyncio
    async def test_valid_cancelled(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-cancel",
            event_id="evt-cancel",
            target_adapter="mesh-1",
            delivery_plan_id="plan-cancel",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-cancel",
            adapter="mesh-1",
            outbox_id="obox-cancel",
            delivery_plan_id="plan-cancel",
            outcome="cancelled",
            error="cancelled in-flight",
        )
        await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-cancel")
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        assert receipts[0].failure_kind == "adapter_transient"

        outbox = await temp_storage.get_outbox_item("obox-cancel")
        assert outbox is not None
        assert outbox.status == "cancelled"


# ===================================================================
# 14. Valid abandoned → receipt + abandoned outbox
# ===================================================================


class TestValidAbandoned:
    """Correct abandoned callback produces receipt and abandoned outbox."""

    @pytest.mark.asyncio
    async def test_valid_abandoned(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-abandon",
            event_id="evt-abandon",
            target_adapter="mesh-1",
            delivery_plan_id="plan-abandon",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-abandon",
            adapter="mesh-1",
            outbox_id="obox-abandon",
            delivery_plan_id="plan-abandon",
            outcome="abandoned",
            error="shutdown drain",
        )
        await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-abandon")
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        assert receipts[0].failure_kind == "adapter_transient"

        outbox = await temp_storage.get_outbox_item("obox-abandon")
        assert outbox is not None
        assert outbox.status == "abandoned"


# ===================================================================
# 15. Valid callback preserves route_id from outbox row
# ===================================================================


class TestValidPreservesRouteId:
    """Terminal receipt must inherit route_id from the outbox row."""

    @pytest.mark.asyncio
    async def test_valid_preserves_route_id(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        await _create_outbox_item(
            temp_storage,
            outbox_id="obox-route",
            event_id="evt-route",
            route_id="route-special-42",
            target_adapter="mesh-1",
            delivery_plan_id="plan-route",
            status="in_progress",
        )

        manager = _make_manager(temp_storage)
        record = QueueTerminalRecord(
            event_id="evt-route",
            adapter="mesh-1",
            outbox_id="obox-route",
            delivery_plan_id="plan-route",
            outcome="exhausted",
            error="budget exhausted",
        )
        await manager.record_terminal(record)

        receipts = await temp_storage.list_receipts_for_event("evt-route")
        assert len(receipts) == 1
        assert (
            receipts[0].route_id == "route-special-42"
        ), f"Expected route_id='route-special-42', got '{receipts[0].route_id}'"
