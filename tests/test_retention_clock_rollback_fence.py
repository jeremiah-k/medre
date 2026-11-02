"""Clock rollback must not make a future-dated retention heartbeat fresh."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from medre.cli.storage_commands import _heartbeat_observation
from medre.core.storage.backend import ContentCollectionTransientError
from medre.core.storage.content_retention import (
    ContentRetentionPolicy,
    RetentionEpoch,
    retention_epoch_observation_age_seconds,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import ContentCollectionWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.content_collection_worker import (
    FakeClock,
    ScriptedCandidateSource,
    ScriptedStorage,
)

_NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _epoch(observed: str | None) -> RetentionEpoch:
    return RetentionEpoch(
        epoch=1,
        mode="relay-only",
        activated_at=(_NOW - timedelta(seconds=60)).isoformat(),
        admission_watermark=None,
        last_observed_active_at=observed,
        activated_by="operator",
    )


def test_observation_age_rejects_future_heartbeat_and_invalid_timestamps() -> None:
    assert retention_epoch_observation_age_seconds(_epoch(_NOW.isoformat()), _NOW) == 0
    assert retention_epoch_observation_age_seconds(
        _epoch((_NOW - timedelta(seconds=30)).isoformat()), _NOW
    ) == 30
    for value in (
        (_NOW + timedelta(microseconds=1)).isoformat(),
        "bad",
        "2026-11-01T12:00:00",
    ):
        assert retention_epoch_observation_age_seconds(_epoch(value), _NOW) is None
    assert retention_epoch_observation_age_seconds(_epoch(None), _NOW) == 60


def test_runtime_epoch_gate_refuses_future_heartbeat() -> None:
    future = _epoch((_NOW + timedelta(hours=2)).isoformat())
    clock = FakeClock(_NOW)
    worker = ContentCollectionWorker(
        ScriptedStorage(epoch=future, clock=clock),
        ScriptedCandidateSource({}),
        ContentRetentionPolicy(mode="relay-only"),
        now=clock,
    )
    assert worker._epoch_is_stale(future, _NOW)
    assert not worker._epoch_is_stale(_epoch(_NOW.isoformat()), _NOW)


def test_operator_freshness_does_not_accept_future_timestamp() -> None:
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    observation, stale, grace = _heartbeat_observation(_epoch(future.isoformat()))
    assert (observation, stale, grace) == ("unknown", None, 3600)


async def test_worker_suspends_without_heartbeat_or_candidate_read_on_clock_rollback() -> None:
    clock = FakeClock(_NOW)
    future = _epoch((_NOW + timedelta(hours=1)).isoformat())
    storage = ScriptedStorage(epoch=future, clock=clock)
    candidates = ScriptedCandidateSource({})
    worker = ContentCollectionWorker(
        storage, candidates, ContentRetentionPolicy(mode="relay-only"), now=clock
    )
    await worker.sweep()
    assert worker.state.awaiting_activation
    assert worker.state.epoch == future.epoch
    assert not storage.opened
    assert not storage.heartbeats
    assert not storage.collect_calls
    assert not candidates.calls


async def test_upstream_sqlite_operational_error_is_typed_for_backoff(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStorage(str(tmp_path / "transient.db"))
    await store.initialize()
    try:
        async def locked(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "_run_in_thread", locked)
        with pytest.raises(ContentCollectionTransientError):
            await store.latest_retention_epoch()
        with pytest.raises(ContentCollectionTransientError):
            await store.page_collection_candidates(
                after=None, limit=1, watermark=None
            )
        with pytest.raises(ContentCollectionTransientError):
            await store.observe_retention_heartbeat(epoch=1, now=_NOW)
    finally:
        await store.close()


async def test_candidate_read_transient_uses_bounded_cycle_backoff() -> None:
    clock = FakeClock(_NOW)
    storage = ScriptedStorage(clock=clock)

    class LockedCandidates(ScriptedCandidateSource):
        async def page_collection_candidates(self, *, after, limit, watermark):
            raise ContentCollectionTransientError("database is locked")

    worker = ContentCollectionWorker(
        storage, LockedCandidates({}),
        ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
        now=clock,
    )
    try:
        await worker.start()
        assert await wait_until(
            lambda: worker._next_sweep_not_before is not None, timeout=5.0
        )
        assert worker.state.degraded
        assert worker._consecutive_transient == 1
        assert worker._next_sweep_not_before == _NOW + timedelta(seconds=2)
        assert storage.heartbeats == [(1, _NOW)]
        assert any(
            e.kind == "transient" and e.event_id is None
            for e in worker.state.recent_errors
        )
    finally:
        await worker.stop()
