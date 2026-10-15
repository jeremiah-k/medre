"""Callback evidence collection waits for the complete configured fanout."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from medre.runtime.run_session.evidence import _poll_for_receipts


async def test_poll_waits_for_later_target_and_queued_receipt(monkeypatch):
    first = SimpleNamespace(route_id="fanout", target_adapter="first", status="sent")
    queued = SimpleNamespace(
        route_id="fanout", target_adapter="second", status="queued"
    )
    second = SimpleNamespace(route_id="fanout", target_adapter="second", status="sent")
    storage = SimpleNamespace(
        list_receipts_for_event=AsyncMock(
            side_effect=[[first], [first, queued], [first, queued, second]]
        )
    )
    monkeypatch.setattr("medre.runtime.run_session.evidence.asyncio.sleep", AsyncMock())
    receipts = await _poll_for_receipts(
        storage,
        "event",
        expected_deliveries={("fanout", "first"), ("fanout", "second")},
    )
    assert receipts == [first, queued, second]
    assert storage.list_receipts_for_event.await_count == 3


async def test_failed_target_is_terminal_evidence():
    failed = SimpleNamespace(route_id="route", target_adapter="target", status="failed")
    storage = SimpleNamespace(list_receipts_for_event=AsyncMock(return_value=[failed]))
    assert await _poll_for_receipts(
        storage, "event", expected_deliveries={("route", "target")}
    ) == [failed]
    assert storage.list_receipts_for_event.await_count == 1
