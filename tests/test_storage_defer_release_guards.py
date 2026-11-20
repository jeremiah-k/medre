from __future__ import annotations

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import make_outbox_item


async def test_defer_release_rejects_blank_worker(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A blank worker identity never releases a reservation."""
    released = await outbox_temp_storage.defer_unsubmitted_outbox_attempt(
        "outbox-any", worker_id="", attempt_number=1, next_attempt_at="2026-01-01"
    )
    assert released is False


async def test_defer_release_rejects_nonpositive_attempt(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Attempt numbers below one are refused before any row is touched."""
    released = await outbox_temp_storage.defer_unsubmitted_outbox_attempt(
        "outbox-any",
        worker_id="worker-1",
        attempt_number=0,
        next_attempt_at="2026-01-01",
    )
    assert released is False


async def test_defer_release_refuses_unknown_outbox(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """An unknown outbox identity refuses release instead of creating state."""
    released = await outbox_temp_storage.defer_unsubmitted_outbox_attempt(
        "outbox-missing",
        worker_id="worker-1",
        attempt_number=1,
        next_attempt_at="2026-01-01",
    )
    assert released is False


async def test_health_backlog_adapters_without_candidates(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """An empty candidate tuple answers empty without touching the index."""
    assert await outbox_temp_storage.health_backlog_adapters(()) == ()


async def test_health_backlog_adapters_reports_lanes_with_current_work(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Only unhealthy lanes holding nonterminal work answer the backlog probe."""
    item = make_outbox_item(delivery_plan_id="plan-backlog", target_adapter="meshcore")
    await outbox_temp_storage.create_outbox_item(item)

    lanes = await outbox_temp_storage.health_backlog_adapters(("matrix", "meshcore"))
    assert lanes == ("meshcore",)
