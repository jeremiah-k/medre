"""Ceiling-sweep failure atomicity and native dispatch liveness under purge.

Two stability properties behind the 2026-11-23 pressure measurement:

- A verification failure mid-sweep rolls the whole tick back: no receipt, no
  partially purged content, and the durable cursor exactly as it was, with
  the next sweep completing every purge.
- A live dispatcher keeps claiming and sending claimable work while a purge
  backlog drains behind it: the ceiling sweep never starves native retry
  processing.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone

import pytest

from medre.config.model import CeilingSweepConfig, RetryConfig
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.ceiling_audit import _admit_with_ceiling
from tests.helpers.initial_outbox import _pipeline, _stage

_CURSOR_KEY = "content_ceiling_enforcement_cursor"


async def _backdate(store: SQLiteStorage, event_id: str, seconds: int = 5) -> None:
    """Backdate one admitted event's ceiling so it is elapsed for sweeps."""
    elapsed = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await store._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        (elapsed.isoformat(timespec="microseconds"), event_id),
    )


async def _seed_elapsed_targets(store: SQLiteStorage, count: int, prefix: str) -> None:
    for index in range(count):
        event_id = f"{prefix}-{index:03}"
        await _admit_with_ceiling(store, event_id)
        await _backdate(store, event_id)


async def _sweep_now(store: SQLiteStorage) -> datetime:
    ceilings = await store._read_all(
        "SELECT hard_ceiling FROM event_content_access_policies", ()
    )
    return max(datetime.fromisoformat(str(row["hard_ceiling"])) for row in ceilings) + (
        timedelta(seconds=1)
    )


async def _meta_cursor(store: SQLiteStorage) -> str | None:
    row = await store._read_one(
        "SELECT value FROM _medre_schema_meta WHERE key=?", (_CURSOR_KEY,)
    )
    return None if row is None else str(row["value"])


async def _body_row_count(store: SQLiteStorage, prefix: str) -> int:
    row = await store._read_one(
        "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id LIKE ?",
        (f"{prefix}-%",),
    )
    assert row is not None
    return int(row["n"])


async def test_failed_purge_rolls_the_whole_tick_back(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "atomic-target"
    await _seed_elapsed_targets(temp_storage, 6, prefix)
    now = await _sweep_now(temp_storage)
    assert await _body_row_count(temp_storage, prefix) == 6
    cursor_before = await _meta_cursor(temp_storage)

    from medre.core.storage.sqlite._ceiling_enforcement import (
        _measure_and_purge as original_purge,
    )

    calls = {"n": 0}

    def exploding_purge(db, event_id):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("synthetic verification failure")
        return original_purge(db, event_id)

    monkeypatch.setattr(
        "medre.core.storage.sqlite._ceiling_enforcement._measure_and_purge",
        exploding_purge,
    )
    with pytest.raises(RuntimeError):
        await temp_storage.enforce_content_ceilings(now=now, limit=25)
    monkeypatch.undo()

    assert calls["n"] == 3
    assert await _body_row_count(temp_storage, prefix) == 6
    assert await _meta_cursor(temp_storage) == cursor_before
    row = await temp_storage._read_one(
        "SELECT COUNT(*) AS n FROM event_content_ceiling_log", ()
    )
    assert row is not None and int(row["n"]) == 0

    # The next sweep completes every purge with the residual battery clean.
    enforced = await temp_storage.enforce_content_ceilings(now=now, limit=25)
    assert len(enforced) == 6
    assert await _body_row_count(temp_storage, prefix) == 0
    audit = await temp_storage.audit_content_ceilings(limit=25)
    assert not audit["elapsed_pending"] and not audit["residual_enforced"]


async def test_dispatcher_keeps_sending_while_the_purge_backlog_drains(
    temp_storage: SQLiteStorage,
) -> None:
    runner, _route, adapters = _pipeline(temp_storage)
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(runner.stop)
        await runner.start()
        claim_events = [f"liveness-claim-{index}" for index in range(5)]
        for event_id in claim_events:
            rows = await _stage(temp_storage, runner, event_id)
            assert rows and all(
                row.status != "sent" for row in rows
            ), "staging completed native handoff before retry worker started"
        await _seed_elapsed_targets(temp_storage, 25, "liveness-target")
        worker = RetryWorker(
            temp_storage,
            runner,
            None,
            initial_dispatch=True,
            enabled=True,
            interval_seconds=0.05,
            batch_size=10,
            stop_timeout_seconds=2.0,
            retry_config=RetryConfig(
                enabled=True,
                interval_seconds=0.05,
                ceiling_sweep=CeilingSweepConfig(
                    enabled=True, limit=2, interval_seconds=0.05
                ),
            ),
        )
        worker.set_available_target_adapters(adapters.keys())
        cleanup.push_async_callback(worker.stop)
        await worker.start()

        async def sent_while_backlog_remains() -> bool:
            if worker.state.ceiling_enforced >= 25:
                return False
            for event_id in claim_events:
                rows = await temp_storage.list_outbox_items_for_event(
                    event_id, metadata_only=True
                )
                if rows and all(row.status == "sent" for row in rows):
                    return True
            return False

        assert await wait_until(
            sent_while_backlog_remains, timeout=30.0, interval=0.01
        ), "no retry handoff completed while purge backlog remained"

        async def drained() -> bool:
            if worker.state.ceiling_enforced != 25:
                return False
            for event_id in claim_events:
                rows = await temp_storage.list_outbox_items_for_event(
                    event_id, metadata_only=True
                )
                if not rows or not all(row.status == "sent" for row in rows):
                    return False
            return True

        assert await wait_until(drained, timeout=30.0, interval=0.02), (
            f"liveness drain incomplete: enforced={worker.state.ceiling_enforced} "
            f"succeeded={worker.state.succeeded}"
        )
    assert worker.state.ceiling_sweep_errors == 0
    for event_id in claim_events:
        rows = await temp_storage.list_outbox_items_for_event(
            event_id, metadata_only=True
        )
        assert rows and all(row.status == "sent" for row in rows)
    audit = await temp_storage.audit_content_ceilings(limit=25)
    assert not audit["elapsed_pending"] and not audit["residual_enforced"]
