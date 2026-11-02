"""Unit tests for the one live, owner-fenced content collection worker.

Covers the worker lifecycle (idempotent start/stop, bounded two-stage stop,
honest abandonment), the activation-epoch gate (first-start auto-open at the
admission high-water, per-sweep forward-only heartbeat, stale-heartbeat
awaiting state), cursor semantics on every error path (transient abort keeps
the cursor, integrity refusals advance past the event, page exhaustion wraps
to None), the transient backoff (doubling, capped at ten intervals), the
fail-closed restriction after consecutive integrity refusals, and the
content-free discipline of state, error entries, and runtime events.

Most cases run against scripted fakes; a dedicated subset drives the real
``SQLiteStorage`` collector and epoch methods with a fake candidate source
over events seeded through the production admission fixtures.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from medre.core.storage.backend import (
    ContentCollectionTransientError,
    StorageError,
)
from medre.core.storage.content_retention import (
    ContentRetentionPolicy,
    RetentionEpoch,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import (
    ContentCollectionWorker,
    ContentCollectionWorkerError,
    ContentCollectionWorkerState,
)
from medre.runtime.events import EventBuffer
from tests.helpers.async_utils import wait_until
from tests.helpers.content_collection_worker import (
    _MARKER,
    _T0,
    FakeClock,
    RecordingStorage,
    ScriptedCandidateSource,
    ScriptedStorage,
    _body_present,
    _detail_of,
    _event_types,
    _fresh_epoch,
    _page,
    _seed_eligible,
    _worker,
)

# WorkerLifecycle


async def test_worker_lifecycle_start_and_stop_are_idempotent() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)

    await worker.start()
    task = worker._task
    assert task is not None
    await worker.start()
    assert worker._task is task, "duplicate start must not spawn a second loop"
    assert await wait_until(lambda: len(source.calls) >= 1)

    await worker.stop()
    # A second stop is a no-op.
    await worker.stop()
    assert worker._task is None
    assert worker.state.running is False
    assert worker.state.abandoned is False
    assert worker.has_retained_work is False
    assert _event_types(buffer).count("collection_started") == 1
    assert _event_types(buffer).count("collection_stopped") == 1


async def test_worker_lifecycle_start_after_clean_stop_launches_new_generation() -> (
    None
):
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({})
    worker = _worker(storage, source, clock=clock)
    await worker.start()
    await worker.stop()
    await worker.start()
    assert worker._task is not None
    assert worker.state.running is True
    await worker.stop()


async def test_worker_lifecycle_stop_is_bounded_for_cancellation_resistant_sweep() -> (
    None
):
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    storage.collect_gate = asyncio.Event()
    storage.collect_cancel_resistant = True
    source = ScriptedCandidateSource({None: _page(["target"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer, stop_timeout=0.1)
    await worker.start()
    original = worker._task
    assert await wait_until(lambda: len(storage.collect_calls) >= 1)

    started = asyncio.get_running_loop().time()
    await worker.stop()
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 1.0, "bounded stop must not wait on a resistant loop"

    assert worker._task is None
    assert worker._abandoned_task is original
    assert worker.state.running is False
    assert worker.state.abandoned is True
    assert worker.has_retained_work is True
    assert "collection_abandoned" in _event_types(buffer)
    assert "collection_stopped" not in _event_types(buffer)
    abandoned_detail = _detail_of(buffer, "collection_abandoned")
    assert abandoned_detail["stop_timeout_seconds"] == 0.1

    # Release the stuck call so the retained task can finish; the done
    # callback must clear the retained reference.
    storage.collect_gate.set()
    assert original is not None
    try:
        await asyncio.wait_for(original, timeout=2.0)
    except asyncio.CancelledError:
        pass
    assert worker.has_retained_work is False


async def test_worker_lifecycle_start_refused_after_abandon() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    storage.collect_gate = asyncio.Event()
    storage.collect_cancel_resistant = True
    source = ScriptedCandidateSource({None: _page(["target"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer, stop_timeout=0.1)
    try:
        await worker.start()
        assert await wait_until(lambda: len(storage.collect_calls) >= 1)
        await worker.stop()
        assert worker.state.abandoned is True

        await worker.start()
        assert worker._task is None, "refused start must not spawn a loop"
        refused = _detail_of(buffer, "collection_start_refused")
        assert refused["reason"] == "abandoned"
    finally:
        storage.collect_gate.set()
        abandoned = worker._abandoned_task
        if abandoned is not None and not abandoned.done():
            try:
                await asyncio.wait_for(abandoned, timeout=2.0)
            except asyncio.CancelledError:
                pass


async def test_worker_lifecycle_stop_cancelled_mid_poll_marks_abandoned_and_reraises() -> (
    None
):
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    storage.collect_gate = asyncio.Event()
    storage.collect_cancel_resistant = True
    source = ScriptedCandidateSource({None: _page(["target"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer, stop_timeout=0.1)
    try:
        await worker.start()
        assert await wait_until(lambda: len(storage.collect_calls) >= 1)
        stop_task = asyncio.create_task(worker.stop())
        # Deterministic hook: stop() sets the shutdown event before its
        # cooperative poll begins, and the resistant loop cannot finish.
        assert await wait_until(lambda: worker._shutdown_event.is_set())
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert worker.state.abandoned is True
        assert worker.has_retained_work is True
        abandoned_detail = _detail_of(buffer, "collection_abandoned")
        assert abandoned_detail["reason"] == "stop_cancelled"
    finally:
        storage.collect_gate.set()
        abandoned = worker._abandoned_task
        if abandoned is not None and not abandoned.done():
            try:
                await asyncio.wait_for(abandoned, timeout=2.0)
            except asyncio.CancelledError:
                pass


async def test_worker_lifecycle_retained_policy_worker_never_starts() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(
        storage,
        source,
        policy=ContentRetentionPolicy(mode="retain"),
        clock=clock,
        buffer=buffer,
    )
    assert worker.state.enabled is False
    await worker.start()
    assert worker._task is None
    assert _event_types(buffer) == []


def test_worker_lifecycle_stop_timeout_must_be_positive() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({})
    with pytest.raises(ValueError, match="stop_timeout_seconds"):
        _worker(storage, source, clock=clock, stop_timeout=0.0)


async def test_worker_lifecycle_cooperative_shutdown_mid_page_skips_collection() -> (
    None
):
    """A shutdown observed mid-page leaves the cursor and counters alone."""
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({None: _page(["a", "b"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)
    worker._shutdown_event.set()
    await worker.sweep()
    assert storage.collect_calls == []
    assert worker._cursor is None
    assert "collection_swept" not in _event_types(buffer)


# ---------------------------------------------------------------------------
# Activation epoch gate and heartbeat
# ---------------------------------------------------------------------------


# EpochGate


async def test_epoch_gate_first_sweep_opens_runtime_first_start_epoch_at_high_water(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "first-start.db"))
    await store.initialize()
    try:
        await _seed_eligible(store, "historic", "2026-10-31T10:00:00+00:00", _MARKER)
        clock = FakeClock()
        source = ScriptedCandidateSource({})
        buffer = EventBuffer(maxlen=64)
        worker = _worker(store, source, clock=clock, buffer=buffer)

        await worker.sweep()

        epoch = await store.latest_retention_epoch()
        assert epoch is not None
        assert epoch.epoch == 1
        assert epoch.activated_by == "runtime-first-start"
        assert epoch.admission_watermark == "2026-10-31T10:00:00+00:00"
        observed = datetime.fromisoformat(epoch.last_observed_active_at or "")
        assert observed == clock.value, "the sweep heartbeated at the clock"
        assert worker.state.epoch == 1
        assert source.calls[0]["watermark"] == "2026-10-31T10:00:00+00:00"
        assert source.calls[0]["limit"] == 25
        # The boundary excludes pre-activation history: the seeded
        # event was never a candidate.
        assert source.calls[0]["after"] is None
        assert await _body_present(store, "historic")
    finally:
        await store.close()


async def test_epoch_gate_boundary_excludes_pre_activation_events(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "boundary.db"))
    await store.initialize()
    recording = RecordingStorage(store)
    try:
        await _seed_eligible(store, "historic", "2026-10-31T10:00:00+00:00", "old")
        clock = FakeClock()
        source = ScriptedCandidateSource({})
        worker = _worker(recording, source, clock=clock)
        await worker.sweep()  # opens the epoch; empty first page

        await _seed_eligible(store, "fresh", "2026-11-02T10:00:00+00:00", _MARKER)
        source.pages = {None: _page(["fresh"])}
        await worker.sweep()

        assert worker.state.collected == 1
        assert worker.state.rows_removed == 1
        assert worker.state.logical_bytes_removed == len(_MARKER)
        assert not await _body_present(store, "fresh")
        assert await _body_present(store, "historic")
        assert recording.collect_calls == [("fresh", False)]
    finally:
        await store.close()


async def test_epoch_gate_heartbeat_is_observed_once_per_sweep_before_paging() -> None:
    clock = FakeClock()
    trace: list[str] = []
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock, trace=trace)
    source = ScriptedCandidateSource({None: _page(["a"])}, trace=trace)
    worker = _worker(storage, source, clock=clock)

    await worker.sweep()
    clock.advance(seconds=5)
    await worker.sweep()

    assert storage.heartbeats == [(1, _T0), (1, _T0 + timedelta(seconds=5))]
    assert trace == [
        "epoch_read",
        "heartbeat:1",
        "page",
        "collect:a",
        "epoch_read",
        "heartbeat:1",
        "page",
        "collect:a",
    ]


async def test_epoch_gate_stale_epoch_heartbeat_suspends_collection(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "stale.db"))
    await store.initialize()
    recording = RecordingStorage(store)
    try:
        stale_now = _T0 - timedelta(days=8)
        await store.open_retention_epoch(activated_by="operator", now=stale_now)
        await _seed_eligible(store, "fresh", "2026-11-02T10:00:00+00:00", _MARKER)
        clock = FakeClock(_T0)
        source = ScriptedCandidateSource({None: _page(["fresh"])})
        buffer = EventBuffer(maxlen=64)
        worker = _worker(recording, source, clock=clock, buffer=buffer)

        await worker.sweep()
        await worker.sweep()

        assert worker.state.awaiting_activation is True
        assert worker.state.epoch == 1
        assert source.calls == [], "stale epoch must not page candidates"
        assert recording.collect_calls == []
        epoch = await store.latest_retention_epoch()
        assert epoch is not None
        assert epoch.last_observed_active_at is None, "no heartbeat while stale"
        assert await _body_present(store, "fresh")
        assert _event_types(buffer).count("collection_epoch_awaited") == 1
        awaited = _detail_of(buffer, "collection_epoch_awaited")
        assert awaited["epoch"] == 1
    finally:
        await store.close()


async def test_epoch_gate_fresh_operator_epoch_resumes_collection(tmp_path) -> None:
    """A fresh operator epoch clears awaiting_activation and is adopted
    on the next sweep; automatic collection then applies to post-
    activation admissions only. The pre-activation admission stays
    retained until the explicit manual path (no epoch fence) collects
    it."""
    store = SQLiteStorage(str(tmp_path / "resume.db"))
    await store.initialize()
    recording = RecordingStorage(store)
    try:
        stale_now = _T0 - timedelta(days=8)
        await store.open_retention_epoch(activated_by="operator", now=stale_now)
        await _seed_eligible(
            store, "pre-activation", "2026-11-02T10:00:00+00:00", _MARKER
        )
        clock = FakeClock(_T0)
        source = ScriptedCandidateSource({})
        buffer = EventBuffer(maxlen=64)
        worker = _worker(recording, source, clock=clock, buffer=buffer)
        await worker.sweep()
        assert worker.state.awaiting_activation is True

        operator = await store.open_retention_epoch(
            activated_by="operator", now=clock.value
        )
        await worker.sweep()

        assert worker.state.awaiting_activation is False
        assert worker.state.epoch == operator.epoch
        assert recording.collect_calls == []
        assert await _body_present(
            store, "pre-activation"
        ), "the pre-activation admission is never auto-collected"

        # A post-activation admission collects automatically.
        await _seed_eligible(
            store, "post-activation", "2026-11-03T10:00:00+00:00", _MARKER
        )
        source.pages = {None: _page(["post-activation"])}
        await worker.sweep()

        assert worker.state.collected == 1
        assert not await _body_present(store, "post-activation")
        assert await _body_present(store, "pre-activation")
        assert recording.collect_calls == [("post-activation", False)]

        # The explicit manual path passes no retention_epoch and has no
        # epoch fence: the escape hatch for pre-activation admissions.
        manual = await store.collect_event_content("pre-activation", dry_run=False)
        assert manual["status"] == "collected"
        assert not await _body_present(store, "pre-activation")
    finally:
        await store.close()


def test_epoch_gate_staleness_boundary_is_exact() -> None:
    """``max(3600, 10 * interval)`` compares strictly: exactly at the
    threshold the epoch is fresh, one microsecond later it is stale.

    Asserted on the gate's comparator directly — a live worker
    heartbeats every sweep while making timely progress. A stalled runtime
    or a deactivated policy can leave that durable observation stale.
    """
    clock = FakeClock()
    storage = ScriptedStorage(clock=clock)
    storage.epoch = RetentionEpoch(
        epoch=3,
        mode="relay-only",
        activated_at="2026-10-01T00:00:00+00:00",
        admission_watermark=None,
        last_observed_active_at="2026-11-01T00:00:00.000000+00:00",
        activated_by="operator",
    )
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=1)
    worker = _worker(storage, ScriptedCandidateSource({}), policy=policy, clock=clock)
    assert worker._epoch_staleness_threshold() == 3600.0

    observed = datetime(2026, 11, 1, tzinfo=timezone.utc)
    at_threshold = observed + timedelta(seconds=3600)
    assert worker._epoch_is_stale(storage.epoch, at_threshold) is False

    past_threshold = at_threshold + timedelta(microseconds=1)
    assert worker._epoch_is_stale(storage.epoch, past_threshold) is True


def test_epoch_gate_staleness_threshold_floor_is_ten_intervals() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(clock=clock)
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=86_400)
    worker = _worker(storage, ScriptedCandidateSource({}), policy=policy, clock=clock)
    assert worker._epoch_staleness_threshold() == 864_000.0


async def test_epoch_gate_unparseable_heartbeat_fails_closed() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(clock=clock)
    storage.epoch = RetentionEpoch(
        epoch=1,
        mode="relay-only",
        activated_at=clock.value.isoformat(),
        admission_watermark=None,
        last_observed_active_at="not-a-timestamp",
        activated_by="operator",
    )
    source = ScriptedCandidateSource({None: _page(["a"])})
    worker = _worker(storage, source, clock=clock)
    await worker.sweep()
    assert worker.state.awaiting_activation is True
    assert source.calls == []


async def test_epoch_gate_naive_heartbeat_fails_closed() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(clock=clock)
    storage.epoch = RetentionEpoch(
        epoch=1,
        mode="relay-only",
        activated_at="2026-10-01T00:00:00+00:00",
        admission_watermark=None,
        last_observed_active_at="2026-10-31T00:00:00",
        activated_by="operator",
    )
    source = ScriptedCandidateSource({None: _page(["a"])})
    worker = _worker(storage, source, clock=clock)
    await worker.sweep()
    assert worker.state.awaiting_activation is True
    assert source.calls == []


def storage_collect_calls(worker: ContentCollectionWorker) -> list[tuple[str, bool]]:
    """Read the collect call log from either the fake or the real storage."""
    storage = worker._storage
    calls = getattr(storage, "collect_calls", None)
    assert calls is not None
    return list(calls)


# ---------------------------------------------------------------------------
# Transient failures: page abort, cursor retention, backoff
# ---------------------------------------------------------------------------


# TransientFailures


async def test_transient_failures_transient_error_aborts_page_and_keeps_cursor() -> (
    None
):
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        results={
            "a": {
                "status": "collected",
                "rows_removed": 1,
                "logical_bytes_removed": 11,
            }
        },
        errors={"b": ContentCollectionTransientError("locked")},
    )
    key = ("2026-11-01T00:00:00+00:00", "c")
    source = ScriptedCandidateSource(
        {None: _page(["a", "b", "c"], next_after=key, has_more=True)}
    )
    buffer = EventBuffer(maxlen=64)
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=10)
    worker = _worker(storage, source, policy=policy, clock=clock, buffer=buffer)

    await worker.sweep()

    assert storage.collect_calls == [("a", False), ("b", False)]
    assert "c" not in [event for event, _ in storage.collect_calls]
    assert worker._cursor is None, "cursor must not advance past the failure"
    assert worker.state.degraded is True
    assert worker.state.events_examined == 2
    assert worker.state.collected == 1
    assert worker._next_sweep_not_before == clock.value + timedelta(seconds=20)
    deferred = _detail_of(buffer, "collection_deferred")
    assert deferred["event_id"] == "b"
    assert deferred["backoff_seconds"] == 20
    assert "collection_swept" not in _event_types(buffer)
    entry = worker.state.recent_errors[-1]
    assert (entry.kind, entry.event_id) == ("transient", "b")


async def test_transient_failures_transient_backoff_doubles_and_caps_at_ten_intervals() -> (
    None
):
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        errors={"x": ContentCollectionTransientError("busy")},
    )
    source = ScriptedCandidateSource({None: _page(["x"])})
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=10)
    worker = _worker(storage, source, policy=policy, clock=clock)

    expected_delays = [20, 40, 80, 100, 100, 100]
    for delay in expected_delays:
        await worker.sweep()
        assert worker._next_sweep_not_before is not None
        actual = (worker._next_sweep_not_before - clock.value).total_seconds()
        assert actual == delay
        clock.advance(seconds=delay)


@pytest.mark.parametrize("interval", [360, 600, 3600, 86400])
async def test_transient_backoff_preserves_heartbeat_freshness(interval: int) -> None:
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        errors={"x": ContentCollectionTransientError("busy")},
    )
    collect = storage.collect_event_content

    async def slow_collect(*args, **kwargs):
        try:
            return await collect(*args, **kwargs)
        finally:
            # The next defer starts after the heartbeat, even on a busy DB.
            clock.advance(seconds=1)

    storage.collect_event_content = slow_collect
    source = ScriptedCandidateSource({None: _page(["x"])})
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=interval)
    worker = _worker(storage, source, policy=policy, clock=clock)
    for attempt in range(1, 6):
        await worker.sweep()
        assert worker.state.awaiting_activation is False
        assert len(storage.collect_calls) == attempt
        delay = (worker._next_sweep_not_before - clock.value).total_seconds()
        assert delay <= worker._epoch_staleness_threshold() - interval
        clock.advance(seconds=delay)
    storage.errors.clear()
    await worker.sweep()
    assert len(storage.collect_calls) == 6
    assert worker.state.awaiting_activation is False
    assert worker.state.degraded is False
    assert worker._next_sweep_not_before is None


async def test_transient_failures_clean_page_clears_backoff_and_degraded() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        errors={"b": ContentCollectionTransientError("locked")},
    )
    source = ScriptedCandidateSource({None: _page(["a", "b"])})
    buffer = EventBuffer(maxlen=64)
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=10)
    worker = _worker(storage, source, policy=policy, clock=clock, buffer=buffer)

    await worker.sweep()
    assert worker.state.degraded is True
    assert worker._next_sweep_not_before is not None

    del storage.errors["b"]
    await worker.sweep()
    assert worker.state.degraded is False
    assert worker._next_sweep_not_before is None
    assert "collection_swept" in _event_types(
        _buffer_of(worker)
    ), "deferred cycle emits no swept event, clean cycle does"

    # The consecutive counter reset: a fresh transient backs off 2x again.
    storage.errors["b"] = ContentCollectionTransientError("locked")
    await worker.sweep()
    assert (worker._next_sweep_not_before - clock.value).total_seconds() == 20


def _buffer_of(worker: ContentCollectionWorker) -> EventBuffer:
    buffer = worker._event_buffer
    assert buffer is not None
    return buffer


# ---------------------------------------------------------------------------
# Integrity refusals: fail-closed counting and restriction
# ---------------------------------------------------------------------------


# IntegrityRefusals


async def test_integrity_refusals_refusal_counts_failed_and_advances_cursor() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        results={
            "good": {
                "status": "collected",
                "rows_removed": 1,
                "logical_bytes_removed": 7,
            },
        },
        errors={"bad": StorageError("Collected content was unexpectedly recreated")},
    )
    key = ("2026-11-01T00:00:00+00:00", "good")
    source = ScriptedCandidateSource(
        {None: _page(["bad", "good"], next_after=key, has_more=True)}
    )
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)

    await worker.sweep()

    assert worker.state.failed == 1
    assert worker.state.collected == 1
    assert worker._cursor == key, "cursor advances past the refused event"
    assert worker.state.restricted is False
    failed = _detail_of(buffer, "collection_failed")
    assert failed["event_id"] == "bad"
    assert failed["error_type"] == "StorageError"
    assert "collection_swept" in _event_types(buffer)
    entry = worker.state.recent_errors[-1]
    assert (entry.kind, entry.event_id) == ("integrity_refused", "bad")


async def test_integrity_refusals_three_consecutive_refusals_in_one_sweep_restrict() -> (
    None
):
    clock = FakeClock()
    refusal = StorageError("Collection requires a valid admission identity")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        errors={f"x{i}": refusal for i in range(1, 5)},
    )
    source = ScriptedCandidateSource({None: _page(["x1", "x2", "x3", "x4"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)

    await worker.sweep()

    assert worker.state.restricted is True
    assert storage.collect_calls == [
        ("x1", False),
        ("x2", False),
        ("x3", False),
    ], "the remaining page is abandoned"
    assert worker.state.events_examined == 3
    assert worker.state.failed == 3
    assert worker._cursor is None
    restricted = _detail_of(buffer, "collection_restricted")
    assert restricted["consecutive_refusals"] == 3
    assert _event_types(buffer).count("collection_restricted") == 1
    assert worker.state.last_outcome is not None
    assert worker.state.last_outcome["failed"] == 3


async def test_integrity_refusals_restricted_worker_stops_loop_and_refuses_start() -> (
    None
):
    clock = FakeClock()
    refusal = StorageError("Collection requires a valid admission identity")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        errors={f"x{i}": refusal for i in range(1, 4)},
    )
    source = ScriptedCandidateSource({None: _page(["x1", "x2", "x3"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)

    await worker.start()
    assert await wait_until(
        lambda: worker.state.restricted
        and worker._task is not None
        and worker._task.done()
    )
    assert worker.state.running is False
    assert "collection_restricted" in _event_types(buffer)

    await worker.start()
    refused = _detail_of(buffer, "collection_start_refused")
    assert refused["reason"] == "restricted"
    assert "collection_started" in _event_types(
        buffer
    ), "the first start did emit; the refused restart must not add one"
    assert _event_types(buffer).count("collection_started") == 1

    await worker.stop()
    assert worker._task is None
    assert _event_types(buffer).count("collection_stopped") == 1


async def test_integrity_refusals_success_resets_consecutive_refusal_count() -> None:
    clock = FakeClock()
    refusal = StorageError("Collected content was unexpectedly recreated")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        results={
            "good": {
                "status": "empty",
                "rows_removed": 0,
                "logical_bytes_removed": 0,
            }
        },
        errors={"bad1": refusal, "bad2": refusal, "bad3": refusal},
    )
    source = ScriptedCandidateSource({None: _page(["bad1", "good", "bad2", "bad3"])})
    worker = _worker(storage, source, clock=clock)

    await worker.sweep()

    assert worker.state.restricted is False
    assert worker.state.failed == 3
    assert storage.collect_calls == [
        ("bad1", False),
        ("good", False),
        ("bad2", False),
        ("bad3", False),
    ]


async def test_integrity_refusals_one_refusal_per_cycle_never_restricts() -> None:
    clock = FakeClock()
    refusal = StorageError("Content collection requires an admitted event")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock), clock=clock, errors={"bad": refusal}
    )
    source = ScriptedCandidateSource({None: _page(["bad"])})
    worker = _worker(storage, source, clock=clock)

    for _ in range(3):
        await worker.sweep()

    assert worker.state.restricted is False
    assert worker.state.failed == 3, "re-examined once per full cycle"


# ---------------------------------------------------------------------------
# Cursor wrap, counters, and the dry-run prohibition
# ---------------------------------------------------------------------------


# CursorAndCounters


async def test_cursor_and_counters_full_cycle_cursor_wrap() -> None:
    clock = FakeClock()
    collected = {
        "status": "collected",
        "rows_removed": 1,
        "logical_bytes_removed": 4,
    }
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        results={event_id: dict(collected) for event_id in ("a", "b", "c")},
    )
    key1 = ("2026-11-01T00:00:00+00:00", "b")
    source = ScriptedCandidateSource(
        {
            None: _page(["a", "b"], next_after=key1, has_more=True),
            key1: _page(["c"], next_after=("2026-11-01T01:00:00+00:00", "c")),
        }
    )
    worker = _worker(storage, source, clock=clock)

    await worker.sweep()
    assert worker._cursor == key1
    await worker.sweep()
    assert worker._cursor is None, "page exhaustion wraps to a new cycle"

    # The next cycle re-pages from scope head; previously collected
    # events come back as the cheap already_collected read outcome.
    storage.results = {
        event_id: {
            "status": "already_collected",
            "rows_removed": 0,
            "logical_bytes_removed": 0,
        }
        for event_id in ("a", "b", "c")
    }
    await worker.sweep()
    assert source.calls[2]["after"] is None
    assert worker.state.already_collected == 2, "a and b re-examined"
    assert worker.state.collected == 3


async def test_cursor_and_counters_counters_and_bytes_accumulate() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock),
        clock=clock,
        results={
            "c1": {
                "status": "collected",
                "rows_removed": 2,
                "logical_bytes_removed": 11,
            },
            "c2": {
                "status": "collected",
                "rows_removed": 1,
                "logical_bytes_removed": 7,
            },
            "ac": {
                "status": "already_collected",
                "rows_removed": 0,
                "logical_bytes_removed": 0,
            },
            "bl": {
                "status": "blocked",
                "rows_removed": 0,
                "logical_bytes_removed": 0,
            },
            "em": {
                "status": "empty",
                "rows_removed": 0,
                "logical_bytes_removed": 0,
            },
        },
    )
    source = ScriptedCandidateSource({None: _page(["c1", "c2", "ac", "bl", "em"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)

    await worker.sweep()

    assert worker.state.events_examined == 5
    assert worker.state.collected == 2
    assert worker.state.already_collected == 1
    assert worker.state.blocked == 1
    assert worker.state.empty == 1
    assert worker.state.rows_removed == 3
    assert worker.state.logical_bytes_removed == 18
    assert worker.state.last_sweep_at == _T0.isoformat()
    assert worker.state.last_outcome == {
        "examined": 5,
        "collected": 2,
        "already_collected": 1,
        "blocked": 1,
        "empty": 1,
        "failed": 0,
    }
    swept = _detail_of(_buffer_of(worker), "collection_swept")
    for key in (
        "examined",
        "collected",
        "already_collected",
        "blocked",
        "empty",
        "failed",
        "cycle_complete",
        "epoch",
    ):
        assert key in swept
    dumped = json.dumps(swept)
    for event_id in ("c1", "c2", "ac", "bl", "em"):
        assert f'"{event_id}"' not in dumped


async def test_cursor_and_counters_collect_is_never_dry_run() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({None: _page(["a", "b"])})
    worker = _worker(storage, source, clock=clock)
    await worker.sweep()
    await worker.sweep()
    calls = storage_collect_calls(worker)
    assert calls, "the sweep must attempt collection"
    assert all(dry is False for _, dry in calls)


async def test_cursor_and_counters_empty_page_still_sweeps_and_resets_cursor() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)
    await worker.sweep()
    assert worker._cursor is None
    assert worker.state.last_sweep_at == _T0.isoformat()
    assert _event_types(_buffer_of(worker)) == ["collection_swept"]


# ---------------------------------------------------------------------------
# Loop resilience and content-free observability
# ---------------------------------------------------------------------------


def _state_json(worker: ContentCollectionWorker) -> str:
    """Serialize the worker state JSON-safely (deque expanded to entries)."""
    payload = dataclasses.asdict(worker.state)
    payload["recent_errors"] = [
        dataclasses.asdict(entry) for entry in worker.state.recent_errors
    ]
    return json.dumps(payload)


# LoopAndObservability


async def test_loop_and_observability_loop_survives_cycle_error_and_recovers() -> None:
    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    storage.fail_epoch_reads = 1
    source = ScriptedCandidateSource({None: _page(["a"])})
    buffer = EventBuffer(maxlen=64)
    policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=1)
    worker = _worker(storage, source, policy=policy, clock=clock, buffer=buffer)
    try:
        await worker.start()
        assert await wait_until(lambda: worker.state.degraded is True)
        assert source.calls == [], "the failed cycle must not have paged"

        assert await wait_until(
            lambda: len(source.calls) >= 1, timeout=5.0
        ), "the loop must survive the cycle error and sweep again"
        assert worker.state.degraded is False
        assert worker.state.empty == 1
    finally:
        await worker.stop()


async def test_loop_and_observability_state_and_events_carry_no_content(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "content-free.db"))
    await store.initialize()
    try:
        clock = FakeClock()
        source = ScriptedCandidateSource({})
        buffer = EventBuffer(maxlen=64)
        worker = _worker(store, source, clock=clock, buffer=buffer)
        # The worker's epoch opens on the empty store first; the event
        # is admitted after that boundary so it is a real candidate.
        await worker.sweep()
        await _seed_eligible(store, "target", "2026-11-02T10:00:00+00:00", _MARKER)
        source.pages = {None: _page(["target"])}
        await worker.sweep()
        assert worker.state.collected == 1

        dumped_state = _state_json(worker)
        dumped_events = json.dumps(buffer.snapshot())
        assert _MARKER not in dumped_state
        assert _MARKER not in dumped_events
    finally:
        await store.close()


async def test_loop_and_observability_error_entries_exclude_exception_message_text() -> (
    None
):
    clock = FakeClock()
    refusal = StorageError(f"refused {_MARKER}")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock), clock=clock, errors={"bad": refusal}
    )
    source = ScriptedCandidateSource({None: _page(["bad"])})
    buffer = EventBuffer(maxlen=64)
    worker = _worker(storage, source, clock=clock, buffer=buffer)
    await worker.sweep()
    dumped = _state_json(worker)
    assert _MARKER not in dumped
    assert _MARKER not in json.dumps(buffer.snapshot())
    entry = worker.state.recent_errors[-1]
    assert isinstance(entry, ContentCollectionWorkerError)
    assert entry.event_id == "bad"
    assert entry.kind == "integrity_refused"


async def test_loop_and_observability_recent_errors_journal_is_bounded() -> None:
    clock = FakeClock()
    refusal = StorageError("refused")
    storage = ScriptedStorage(
        epoch=_fresh_epoch(clock), clock=clock, errors={"bad": refusal}
    )
    source = ScriptedCandidateSource({None: _page(["bad"])})
    worker = _worker(storage, source, clock=clock)
    for _ in range(12):
        await worker.sweep()
    assert len(worker.state.recent_errors) == 8
    assert all(e.kind == "integrity_refused" for e in worker.state.recent_errors)


async def test_loop_and_observability_end_to_end_real_storage_collects_seeded_event(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "end-to-end.db"))
    await store.initialize()
    try:
        clock = FakeClock()
        source = ScriptedCandidateSource({})
        buffer = EventBuffer(maxlen=64)
        policy = ContentRetentionPolicy(mode="relay-only", interval_seconds=1)
        worker = _worker(store, source, policy=policy, clock=clock, buffer=buffer)

        # The started worker's epoch opens on the empty store first; the
        # seeded event is admitted after that boundary.
        await worker.start()
        assert await wait_until(lambda: worker.state.epoch is not None)
        await _seed_eligible(store, "post", "2026-11-02T10:00:00+00:00", _MARKER)
        source.pages = {None: _page(["post"])}

        async def _collected() -> bool:
            return not await _body_present(store, "post")

        assert await wait_until(_collected)
        assert worker.state.collected == 1
        assert worker.state.rows_removed == 1
        assert worker.state.logical_bytes_removed == len(_MARKER)
    finally:
        await worker.stop()
        await store.close()
    assert _event_types(buffer)[:2] == ["collection_started", "collection_swept"]
    assert "collection_stopped" in _event_types(buffer)


# ---------------------------------------------------------------------------
# State dataclass surface
# ---------------------------------------------------------------------------


# WorkerStateSurface


def test_worker_state_surface_defaults_are_inert() -> None:
    state = ContentCollectionWorkerState()
    assert state.enabled is False
    assert state.running is False
    assert state.abandoned is False
    assert state.restricted is False
    assert state.awaiting_activation is False
    assert state.degraded is False
    assert state.last_sweep_at is None
    assert state.epoch is None
    assert state.last_outcome is None
    assert len(state.recent_errors) == 0


def test_worker_state_surface_counter_snapshot_shape() -> None:
    state = ContentCollectionWorkerState()
    state.collected = 2
    state.logical_bytes_removed = 9
    snapshot = state.counter_snapshot()
    assert snapshot == {
        "events_examined": 0,
        "collected": 2,
        "already_collected": 0,
        "blocked": 0,
        "empty": 0,
        "failed": 0,
        "rows_removed": 0,
        "logical_bytes_removed": 9,
    }


async def test_rotated_epoch_during_collection_resets_cursor_without_restriction() -> (
    None
):
    """Operator activation after paging is not an integrity refusal."""
    from medre.core.storage.backend import ContentCollectionEpochChanged

    clock = FakeClock()
    storage = ScriptedStorage(epoch=_fresh_epoch(clock), clock=clock)
    source = ScriptedCandidateSource({None: _page(["candidate"])})
    worker = _worker(storage, source, clock=clock)
    original_collect = storage.collect_event_content
    rotated = False

    async def rotate_on_collect(
        event_id: str, *, dry_run: bool = True, retention_epoch: int | None = None
    ) -> dict[str, Any]:
        nonlocal rotated
        if not rotated:
            rotated = True
            await storage.open_retention_epoch(activated_by="operator", now=clock.value)
        return await original_collect(
            event_id, dry_run=dry_run, retention_epoch=retention_epoch
        )

    storage.collect_event_content = rotate_on_collect  # type: ignore[method-assign]
    await worker.sweep()
    assert rotated
    assert worker.state.collected == 0
    assert worker.state.failed == 0
    assert not worker.state.restricted
    assert worker._cursor is None and worker._cursor_epoch is None
    assert storage.epoch is not None and storage.epoch.epoch == 2
    with pytest.raises(ContentCollectionEpochChanged):
        await original_collect("candidate", dry_run=False, retention_epoch=1)

    await worker.sweep()
    assert worker.state.epoch == 2
    assert worker.state.empty == 1
    assert worker._cursor_epoch == 2
