"""Ceiling-sweep poll wiring under a controlled clock, on real storage.

The failed-sweep recovery cycle is scheduled on the worker's own clock;
the ceiling sweep runs after deadline maintenance and before the retry
claim; the guarded purge removes the frozen dispatch snapshot and leaves
the admission-time route retry bounds in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.config.model import CeilingSweepConfig, RetryConfig
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    OutboxRouteRetryBoundsWrite,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.initial_outbox import _pipeline
from tests.helpers.storage_outbox import admit_event

_INTERVAL = 10


def _sweep_worker(
    temp_storage: SQLiteStorage,
    *,
    sweep: AsyncMock,
    claims: AsyncMock | None = None,
) -> RetryWorker:
    if claims is not None:
        temp_storage.claim_due_outbox_items = claims  # type: ignore[method-assign]
    temp_storage.enforce_content_ceilings = sweep  # type: ignore[method-assign]
    runner, _, _ = _pipeline(temp_storage)
    return RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            ceiling_sweep=CeilingSweepConfig(enabled=True, limit=10)
        ),
        initial_dispatch=True,
        interval_seconds=_INTERVAL,
    )


class _FrozenClock(datetime):
    """A datetime whose now() is pinned for the retry worker module."""

    pinned: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.pinned


async def test_sweep_recovery_survives_a_wall_clock_pause(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed sweep is re-run by the recovery cycle on the worker's clock."""
    import medre.runtime.retry as retry_module

    fixed = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    _FrozenClock.pinned = fixed
    monkeypatch.setattr(retry_module, "datetime", _FrozenClock)
    sweep = AsyncMock(side_effect=RuntimeError("synthetic ceiling sweep failure"))
    claims = AsyncMock(return_value=[])
    worker = _sweep_worker(temp_storage, sweep=sweep, claims=claims)

    await worker._process_due(fixed)
    assert worker.state.ceiling_sweep_errors == 1
    assert claims.await_count == 1

    sweep.side_effect = None
    sweep.return_value = []
    await worker._process_due(fixed + timedelta(seconds=61))
    assert worker.state.ceiling_sweep_runs == 1
    assert worker.state.ceiling_sweep_errors == 1
    assert claims.await_count == 2


async def test_ceiling_sweep_runs_after_deadline_step_and_before_claim(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline step that advances the clock cannot skip or reorder the sweep."""
    order: list[str] = []
    sweep = AsyncMock(return_value=[])

    async def _recording_sweep(*, now=None, limit=50):
        order.append("sweep")
        return await sweep(now=now, limit=limit)

    claims = AsyncMock(return_value=[])

    async def _recording_claim(*args, **kwargs):
        order.append("claim")
        return await claims(*args, **kwargs)

    worker = _sweep_worker(
        temp_storage,
        sweep=AsyncMock(side_effect=_recording_sweep),
        claims=AsyncMock(side_effect=_recording_claim),
    )
    fixed = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    # The sweep is not due when the poll starts.
    worker._next_ceiling_sweep_at = fixed + timedelta(minutes=5)

    async def _advancing_deadline(poll_now: datetime) -> None:
        order.append("deadline")
        # Deadline maintenance advanced the controlled clock past the
        # sweep's due instant.
        worker._next_ceiling_sweep_at = poll_now

    monkeypatch.setattr(worker, "_run_deadline_sweep", _advancing_deadline)

    await worker._process_due(fixed)

    assert order == ["deadline", "sweep", "claim"]
    assert worker.state.ceiling_sweep_runs == 1


async def test_real_sweep_purges_snapshot_but_keeps_bounds(
    temp_storage: SQLiteStorage,
) -> None:
    """The guarded purge removes the frozen context; admission bounds survive."""
    outbox_id = "obox-sweep-bounds"
    await admit_event(temp_storage, "evt-sweep-bounds")
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id="evt-sweep-bounds",
            route_id="route-sweep-bounds",
            delivery_plan_id="plan-sweep-bounds",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=1,
            status="pending",
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(kind="direct", context={}),
        route_retry_bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=5,
            backoff_base=2.0,
            max_delay_seconds=90.0,
            jitter=True,
        ),
    )
    before = await temp_storage.get_outbox_route_retry_bounds(outbox_id)
    assert before is not None
    assert before.jitter is True
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id = ?",
            (outbox_id,),
        )
        is not None
    )

    now = await _install_elapsed_policy(temp_storage, "evt-sweep-bounds")
    await temp_storage.enforce_content_ceilings(now=now)

    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_outbox_snapshots WHERE outbox_id = ?",
            (outbox_id,),
        )
        is None
    )
    after = await temp_storage.get_outbox_route_retry_bounds(outbox_id)
    assert after == before
