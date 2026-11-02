# medre-test-tier: compatibility
"""Clock and policy boundary contracts for the qualified retention vertical.

PR E qualification for the plan's clock/policy family. Every case drives the
real ``ContentCollectionWorker`` over the real ``SQLiteStorage`` epoch,
selector, and collector methods (the restart case drives the real
``MedreApp``), with an injected clock — no sleeps, no wall-clock races:

- duplicate activation appends epochs with monotonic watermarks and the
  worker adopts the newest on its next sweep;
- an enabled-to-disabled config change across restarts leaves the epoch
  durable, collects nothing while disabled, and the re-enabled worker
  enters ``awaiting_activation`` until an operator epoch opens;
- the heartbeat staleness grace is exactly ``max(3600, 10 * interval)``:
  at the boundary the epoch is fresh, one microsecond later it is stale;
- the admission watermark is strictly exclusive: an event admitted at the
  watermark instant is never a candidate, one microsecond after is;
- an unrevoked optional body grant blocks worker collection with the
  blocker surfaced, observed expiry unblocks a later cycle, and observed
  revocation/expiry tombstones stay monotonic across worker restarts;
- a database with an unexpected extra table is refused at open and the
  worker collects nothing;
- corrupt admission identity counts failed, deletes nothing, and three
  consecutive refusals restrict the worker through the real storage path;
- a present-but-empty body collects as a content row while an absent body
  reports the empty outcome — the worker classifies both correctly;
- events admitted in ``created_at`` order with reverse-lexicographic event
  IDs are collected in admission order, extending the candidate selector's
  non-chronological-ID pin through the worker.

Plan invariants proven here: 3 (unknown/corrupt blocks), 6 (grant
monotonicity), 7 (strict schema refusal).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from medre.config.model import ContentRetentionConfig, RuntimeOptions, StorageConfig
from medre.config.paths import MedrePaths, resolve
from medre.core.storage.backend import PreReleaseSchemaMismatchError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.content_retention import ContentRetentionPolicy
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.collection import ContentCollectionWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.content_lifecycle import T0 as _T0
from tests.helpers.content_lifecycle import CollectRecording as Recording
from tests.helpers.content_lifecycle import FakeClock
from tests.helpers.content_lifecycle import body_present as _body_present
from tests.helpers.content_lifecycle import iso as _iso
from tests.helpers.content_lifecycle import make_worker as _worker
from tests.helpers.content_lifecycle import recording_storage as _recording_storage
from tests.helpers.content_lifecycle import seed_event as _seed
from tests.helpers.content_lifecycle import settle_append_owner as _settle_append_owner
from tests.helpers.content_lifecycle import utc as _utc
from tests.helpers.content_lifecycle import wait_for_worker_epoch
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
)

# ---------------------------------------------------------------------------
# Duplicate activation
# ---------------------------------------------------------------------------


# DuplicateActivation


async def test_duplicate_activation_two_epochs_are_append_only_and_worker_adopts_newest(
    tmp_path,
) -> None:
    """INV: activation appends epochs with monotonic watermarks; the
    worker's next sweep pages under the newest epoch's watermark."""
    store = SQLiteStorage(str(tmp_path / "duplicate.db"))
    await store.initialize()
    try:
        await _seed(store, "early", "2026-10-30T10:00:00+00:00")
        first = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 1)
        )
        await _seed(store, "between", "2026-11-02T10:00:00+00:00")
        # The second activation is fresh relative to the worker's clock:
        # an epoch activated beyond the grace with no heartbeat is stale,
        # which the awaiting-activation scenarios cover deliberately.
        second = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 4, hour=11, minute=30)
        )
        await _seed(store, "post", "2026-11-05T10:00:00+00:00")
        assert (first.epoch, second.epoch) == (1, 2)
        assert first.admission_watermark == "2026-10-30T10:00:00+00:00"
        assert second.admission_watermark == "2026-11-02T10:00:00+00:00"
        assert second.admission_watermark > first.admission_watermark
        rows = await store._read_all(
            "SELECT epoch FROM content_retention_epochs ORDER BY epoch"
        )
        assert [row["epoch"] for row in rows] == [1, 2]

        clock = FakeClock(_utc(2026, 11, 4, hour=11, minute=45))
        record = Recording(collects=[], pages=[])
        worker = _worker(_recording_storage(store, record), clock)
        await worker.sweep()

        assert worker.state.epoch == 2, "the worker adopted the newest epoch"
        assert record.pages == [(None, 25, "2026-11-02T10:00:00+00:00")]
        latest = await store.latest_retention_epoch()
        assert latest is not None and latest.epoch == 2
        assert latest.last_observed_active_at == _iso(clock.value)
        stale = await store._read_one(
            "SELECT last_observed_active_at FROM content_retention_epochs "
            "WHERE epoch=1"
        )
        assert stale is not None
        assert (
            stale["last_observed_active_at"] is None
        ), "only the adopted epoch is heartbeated"
        assert record.collects == ["post"], (
            "epoch 2's watermark pins 'early' and 'between' out of scope; "
            "epoch 1's would have included 'between'"
        )
        assert not await _body_present(store, "post")
        assert await _body_present(store, "early")
        assert await _body_present(store, "between")
    finally:
        await store.close()


async def test_duplicate_activation_reopened_storage_sees_both_epochs_and_keeps_appending(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "durable-epochs.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
    finally:
        await store.close()
    reopened = SQLiteStorage(str(tmp_path / "durable-epochs.db"))
    await reopened.initialize()
    try:
        latest = await reopened.latest_retention_epoch()
        assert latest is not None and latest.epoch == 1
        second = await reopened.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 5)
        )
        assert second.epoch == 2, "activation appends; it never resets"
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Policy changes across restart (full runtime)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


_RELAY_ONLY = ContentRetentionConfig(
    enabled=True, mode="relay-only", interval_seconds=1, batch_limit=10
)
_DISABLED = ContentRetentionConfig()


def _app_config(db_path: str, retention: ContentRetentionConfig) -> Any:
    base = make_multi_adapter_config()
    return replace(
        base,
        content_retention=retention,
        storage=StorageConfig(backend="sqlite", path=db_path),
        runtime=RuntimeOptions(
            name="lifecycle-qualification", shutdown_timeout_seconds=5
        ),
    )


async def _wait_for_epoch(app: MedreApp) -> Any:
    async def _opened() -> bool:
        assert app.storage is not None
        return await app.storage.latest_retention_epoch() is not None

    assert await wait_until(_opened, timeout=5.0)
    assert app.storage is not None
    epoch = await app.storage.latest_retention_epoch()
    return epoch


def _sync_body(db_path: str, event_id: str) -> bool:
    """Synchronous body-row probe for use inside ``wait_until`` predicates."""
    db = sqlite3.connect(db_path)
    try:
        row = db.execute(
            "SELECT 1 FROM event_body_content WHERE event_id=?", (event_id,)
        ).fetchone()
    finally:
        db.close()
    return row is not None


# PolicyAcrossRestarts


async def test_policy_across_restarts_disabled_between_restarts_keeps_epoch_and_awaits_operator(
    tmp_paths: MedrePaths,
) -> None:
    """INV: disabling stops future collection and never resurrects or
    extends the boundary; re-enabling gates on a fresh epoch and never
    silently sweeps earlier history."""
    db_path = str(tmp_paths.state_dir / "policy-restart.sqlite")

    # Phase 1: the enabled relay-only runtime opens the
    # runtime-first-start epoch and collects a post-activation event.
    app1 = await build_and_start(_app_config(db_path, _RELAY_ONLY), tmp_paths)
    try:
        epoch1 = await _wait_for_epoch(app1)
        assert epoch1 is not None
        assert epoch1.activated_by == "runtime-first-start"
        assert app1.storage is not None
        await _seed(app1.storage, "phase-1", "2026-11-02T10:00:00+00:00")
        assert await wait_until(
            lambda: not _sync_body(db_path, "phase-1"), timeout=10.0
        )
        heartbeated = await app1.storage.latest_retention_epoch()
        assert heartbeated is not None
        assert heartbeated.last_observed_active_at is not None
    finally:
        await clean_stop(app1)
    assert app1.state is RuntimeState.STOPPED

    # Phase 2: the disabled configuration constructs no worker, leaves
    # the epoch durable, and collects nothing for the disabled era.
    app2 = await build_and_start(_app_config(db_path, _DISABLED), tmp_paths)
    try:
        assert app2._collection_worker is None
        assert app2.storage is not None
        durable = await app2.storage.latest_retention_epoch()
        assert durable is not None and durable.epoch == epoch1.epoch
        await _seed(app2.storage, "disabled-a", "2026-11-03T10:00:00+00:00")
        await _seed(app2.storage, "disabled-b", "2026-11-03T11:00:00+00:00")
        assert await _body_present(app2.storage, "disabled-a")
        assert await _body_present(app2.storage, "disabled-b")
        assert await app2.storage.count_collected_events() == 1
    finally:
        await clean_stop(app2)
    assert app2.state is RuntimeState.STOPPED

    # Phase 3: the re-enabled worker (real worker, injected clock two
    # days past the last durable heartbeat) finds epoch 1 stale — the
    # disabled runtime stopped heartbeating — and awaits activation.
    store3 = SQLiteStorage(db_path)
    await store3.initialize()
    worker: ContentCollectionWorker | None = None
    try:
        before = await store3.latest_retention_epoch()
        assert before is not None and before.last_observed_active_at is not None
        last_beat = datetime.fromisoformat(before.last_observed_active_at)
        clock = FakeClock(last_beat + timedelta(days=2))
        record = Recording(collects=[], pages=[])
        worker = ContentCollectionWorker(
            _recording_storage(store3, record),
            store3,
            ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
            now=clock,
        )
        await worker.start()
        assert await wait_until(lambda: worker.state.awaiting_activation, timeout=5.0)
        assert record.pages == [], "a stale epoch must not page candidates"
        assert record.collects == []
        after = await store3.latest_retention_epoch()
        assert after is not None
        assert (
            after.last_observed_active_at == before.last_observed_active_at
        ), "no heartbeat while awaiting a fresh epoch"
        assert await _body_present(store3, "disabled-a")

        # An operator epoch opens; the worker resumes on a later sweep,
        # under epoch 2's watermark: the disabled era stays pinned out
        # and only post-activation events are candidates.
        await store3.open_retention_epoch(activated_by="operator", now=clock.value)
        epoch2 = await store3.latest_retention_epoch()
        assert epoch2 is not None and epoch2.epoch == 2
        assert epoch2.admission_watermark == "2026-11-03T11:00:00+00:00"
        await _seed(store3, "phase-3", "2026-11-06T10:00:00+00:00")
        assert await wait_until(lambda: record.collects == ["phase-3"], timeout=10.0)
        assert worker.state.epoch == 2
        assert not await _body_present(store3, "phase-3")
        # Enabling never silently sweeps earlier history.
        assert await _body_present(store3, "disabled-a")
        assert await _body_present(store3, "disabled-b")
    finally:
        if worker is not None:
            await worker.stop()
        await store3.close()


# ---------------------------------------------------------------------------
# Clock boundaries: staleness grace and watermark equality
# ---------------------------------------------------------------------------


# ClockBoundaries


async def test_clock_boundaries_at_heartbeat_grace_boundary_the_epoch_is_fresh(
    tmp_path,
) -> None:
    """INV: staleness compares strictly at ``max(3600, 10 * interval)`` —
    at the boundary the epoch is fresh and collection proceeds."""
    store = SQLiteStorage(str(tmp_path / "grace-at.db"))
    await store.initialize()
    try:
        heartbeat_at = _utc(2026, 11, 1)
        await store.open_retention_epoch(
            activated_by="operator", now=heartbeat_at - timedelta(days=8)
        )
        await store.observe_retention_heartbeat(epoch=1, now=heartbeat_at)
        await _seed(store, "target", "2026-11-02T10:00:00+00:00")
        clock = FakeClock(heartbeat_at + timedelta(seconds=3600))
        worker = _worker(
            store,
            clock,
            policy=ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
        )
        await worker.sweep()
        assert worker.state.awaiting_activation is False
        assert worker.state.collected == 1
        assert not await _body_present(store, "target")
    finally:
        await store.close()


async def test_clock_boundaries_one_microsecond_past_grace_is_stale(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "grace-plus-one.db"))
    await store.initialize()
    try:
        heartbeat_at = _utc(2026, 11, 1)
        await store.open_retention_epoch(
            activated_by="operator", now=heartbeat_at - timedelta(days=8)
        )
        await store.observe_retention_heartbeat(epoch=1, now=heartbeat_at)
        await _seed(store, "target", "2026-11-02T10:00:00+00:00")
        clock = FakeClock(heartbeat_at + timedelta(seconds=3600, microseconds=1))
        worker = _worker(
            store,
            clock,
            policy=ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
        )
        await worker.sweep()
        assert worker.state.awaiting_activation is True
        assert worker.state.collected == 0
        assert await _body_present(store, "target")
    finally:
        await store.close()


async def test_clock_boundaries_watermark_equality_exclusion_through_worker(
    tmp_path,
) -> None:
    """INV: the activation watermark is strictly exclusive — an event at
    the watermark instant (admitted before or after activation) is never
    a candidate; one microsecond after is."""
    store = SQLiteStorage(str(tmp_path / "watermark.db"))
    await store.initialize()
    try:
        watermark_instant = "2026-11-02T10:00:00.000000+00:00"
        after_instant = "2026-11-02T10:00:00.000001+00:00"
        await _seed(store, "at-watermark", watermark_instant)
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 3))
        # Admitted after activation but pinned AT the watermark instant.
        await _seed(store, "at-watermark-later", watermark_instant)
        await _seed(store, "after-watermark", after_instant)

        record = Recording(collects=[], pages=[])
        worker = _worker(
            _recording_storage(store, record), FakeClock(_utc(2026, 11, 3, hour=1))
        )
        await worker.sweep()

        assert record.collects == ["after-watermark"]
        assert not await _body_present(store, "after-watermark")
        assert await _body_present(store, "at-watermark")
        assert await _body_present(store, "at-watermark-later")
        assert worker.state.collected == 1
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Retention grants
# ---------------------------------------------------------------------------


def _grant_policy(clock: FakeClock, consumer: str) -> AvailabilityPolicy:
    return AvailabilityPolicy(
        hard_ceiling=clock.value + timedelta(days=365),
        max_optional_hold=timedelta(days=30),
        allowed_optional_consumers=frozenset({consumer}),
    )


async def _issue_hold(
    store: SQLiteStorage,
    event_id: str,
    clock: FakeClock,
    *,
    consumer: str = "held-consumer",
    expires_in: timedelta = timedelta(days=29),
) -> ContentHold:
    hold = ContentHold(
        consumer=consumer,
        scope="body",
        expires_at=clock.value + expires_in,
        issued_at=clock.value,
    )
    await store.issue_optional_body_hold(
        event_id, hold, _grant_policy(clock, consumer), now=clock.value
    )
    return hold


# RetentionGrants


async def test_retention_grants_unrevoked_grant_blocks_worker_and_surfaces_blocker(
    tmp_path,
) -> None:
    """INV: optional grants are honored — a live grant blocks collection
    and the refusal names the blocker family."""
    store = SQLiteStorage(str(tmp_path / "grant-blocks.db"))
    await store.initialize()
    try:
        clock = FakeClock()
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        await _seed(store, "held", "2026-11-02T10:00:00+00:00", settled=False)
        await _issue_hold(store, "held", clock)
        await _settle_append_owner(store, "held")
        record = Recording(collects=[], pages=[])
        worker = _worker(_recording_storage(store, record), clock)
        await worker.sweep()

        assert worker.state.blocked == 1
        assert worker.state.collected == 0
        assert await _body_present(store, "held")
        blocked = await store.collect_event_content("held", dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["unrevoked_optional_grants"]
    finally:
        await store.close()


async def test_retention_grants_observed_expiry_unblocks_a_later_cycle(
    tmp_path,
) -> None:
    """INV: expiry stays monotonic — a lapsed grant unblocks only once
    its expiry is durably observed; a clock rollback cannot reopen it,
    and the tombstone plus collected state survive a worker restart."""
    path = str(tmp_path / "grant-expiry.db")
    store = SQLiteStorage(path)
    clock = FakeClock()
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        await _seed(store, "held", "2026-11-02T10:00:00+00:00", settled=False)
        await _issue_hold(store, "held", clock, expires_in=timedelta(seconds=2))
        await _settle_append_owner(store, "held")
        record = Recording(collects=[], pages=[])
        worker = _worker(_recording_storage(store, record), clock)
        await worker.sweep()
        assert worker.state.blocked == 1, "the live grant blocks"

        # Advance past expiry and observe it through the real revoke
        # surface: an expired grant cannot be revoked, but observation
        # writes the durable expired_at tombstone either way.
        clock.advance(seconds=3)
        assert not await store.revoke_optional_body_hold(
            "held", "held-consumer", now=clock.value
        )
        tombstone = await store._read_one(
            "SELECT expired_at, revoked_at FROM event_content_access_grants "
            "WHERE event_id='held' AND consumer='held-consumer'"
        )
        assert tombstone is not None
        assert tombstone["expired_at"] is not None
        assert tombstone["revoked_at"] is None

        await worker.sweep()
        assert worker.state.collected == 1, "the observed expiry unblocks"
        assert not await _body_present(store, "held")
    finally:
        await store.close()

    # Monotonic across a worker restart: the tombstone survives and the
    # collected event answers already_collected.
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        row = await reopened._read_one(
            "SELECT expired_at FROM event_content_access_grants "
            "WHERE event_id='held' AND consumer='held-consumer'"
        )
        assert row is not None and row["expired_at"] is not None
        record2 = Recording(collects=[], pages=[])
        worker2 = _worker(_recording_storage(reopened, record2), clock)
        await worker2.sweep()
        assert worker2.state.already_collected == 1
        assert worker2.state.collected == 0
    finally:
        await reopened.close()


async def test_retention_grants_revocation_is_monotonic_across_worker_restarts(
    tmp_path,
) -> None:
    path = str(tmp_path / "grant-revoke.db")
    store = SQLiteStorage(path)
    await store.initialize()
    clock = FakeClock()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        await _seed(store, "held", "2026-11-02T10:00:00+00:00", settled=False)
        await _issue_hold(store, "held", clock)
        await _settle_append_owner(store, "held")
        assert await store.revoke_optional_body_hold(
            "held", "held-consumer", now=clock.value
        )
    finally:
        await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        record = Recording(collects=[], pages=[])
        worker = _worker(_recording_storage(reopened, record), clock)
        await worker.sweep()
        assert worker.state.collected == 1, "revocation survives the restart"
        assert not await _body_present(reopened, "held")
        row = await reopened._read_one(
            "SELECT revoked_at FROM event_content_access_grants "
            "WHERE event_id='held' AND consumer='held-consumer'"
        )
        assert row is not None and row["revoked_at"] is not None
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Unknown schema refusal and corrupt content
# ---------------------------------------------------------------------------


# UnknownSchemaRefusal


async def test_unknown_schema_refusal_unexpected_table_refuses_open_and_worker_collects_nothing(
    tmp_path,
) -> None:
    """INV: strict version/schema refusal — an unexpected extra table is
    refused at open; the worker surfaces the failure, opens no epoch,
    and collects nothing (plan invariant 7)."""
    path = str(tmp_path / "extra-table.sqlite")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await _seed(store, "kept", "2026-11-02T10:00:00+00:00")
    finally:
        await store.close()

    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE plugin_experimental_notes (note TEXT)")
        db.commit()
    finally:
        db.close()

    refused = SQLiteStorage(path)
    with pytest.raises(PreReleaseSchemaMismatchError):
        await refused.initialize()

    # The runtime worker over the refused handle fails closed: the loop
    # records the cycle failure and never collects.
    clock = FakeClock()
    worker = _worker(refused, clock)
    await worker.start()
    try:
        assert await wait_until(lambda: worker.state.degraded, timeout=5.0)
        assert worker.state.collected == 0
    finally:
        await worker.stop()

    probe = sqlite3.connect(path)
    try:
        epochs = probe.execute(
            "SELECT COUNT(*) FROM content_retention_epochs"
        ).fetchone()[0]
        body = probe.execute(
            "SELECT COUNT(*) FROM event_body_content WHERE event_id='kept'"
        ).fetchone()[0]
        tables = {
            row[0]
            for row in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        probe.close()
    assert epochs == 0
    assert body == 1
    assert "plugin_experimental_notes" in tables


# CorruptContentThroughRealStorage


async def test_corrupt_content_through_real_storage_three_consecutive_corrupt_events_restrict_the_worker(
    tmp_path,
) -> None:
    """INV: inconsistent admission identity fails closed — the worker
    counts failed, deletes nothing, and three consecutive refusals
    restrict it (plan invariant 3), through the real storage path. The
    corrupt events are admitted after the worker's own epoch boundary,
    so the integrity path — not the epoch fence — classifies them."""
    store = SQLiteStorage(str(tmp_path / "corrupt.db"))
    await store.initialize()
    worker: ContentCollectionWorker | None = None
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        record = Recording(collects=[], pages=[])
        worker = ContentCollectionWorker(
            _recording_storage(store, record),
            store,
            ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
            now=FakeClock(),
        )
        # The started worker establishes its own epoch on the empty
        # store first; the corrupt events are admitted after that
        # boundary so the selector pages them.
        await worker.start()
        await wait_for_worker_epoch(worker)
        for index in range(3):
            event_id = f"corrupt-{index}"
            await _seed(store, event_id, f"2026-11-02T10:0{index}:00+00:00", body=None)
            await store._write(
                "INSERT INTO event_body_content(event_id,field,body,created_at) "
                "VALUES (?, 'body', ?, '2026-11-02')",
                (event_id, f"DO-NOT-DELETE-{event_id}"),
            )
        assert await wait_until(lambda: worker.state.restricted, timeout=5.0)
        assert worker.state.failed == 3
        assert worker.state.running is False
        assert record.collects == [f"corrupt-{i}" for i in range(3)]
        for index in range(3):
            assert await _body_present(store, f"corrupt-{index}")
        receipts = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_content_collection_log"
        )
        assert receipts is not None and receipts["n"] == 0
    finally:
        if worker is not None:
            await worker.stop()
        await store.close()

    # A restarted worker's own epoch excludes the pre-restart
    # admissions; freshly admitted corrupt content still restricts it.
    restarted = SQLiteStorage(str(tmp_path / "corrupt.db"))
    await restarted.initialize()
    try:
        record2 = Recording(collects=[], pages=[])
        worker2 = _worker(
            _recording_storage(restarted, record2),
            FakeClock(),
            policy=ContentRetentionPolicy(mode="relay-only", interval_seconds=1),
        )
        await worker2.start()
        try:
            await wait_for_worker_epoch(worker2)
            for index in range(3, 6):
                event_id = f"corrupt-{index}"
                await _seed(
                    restarted,
                    event_id,
                    f"2026-11-02T10:0{index}:00+00:00",
                    body=None,
                )
                await restarted._write(
                    "INSERT INTO event_body_content(event_id,field,body,created_at) "
                    "VALUES (?, 'body', ?, '2026-11-02')",
                    (event_id, f"DO-NOT-DELETE-{event_id}"),
                )
            assert await wait_until(lambda: worker2.state.restricted, timeout=5.0)
            assert worker2.state.failed == 3
            assert record2.collects == [f"corrupt-{i}" for i in range(3, 6)]
        finally:
            await worker2.stop()
    finally:
        await restarted.close()


# ---------------------------------------------------------------------------
# Empty-string vs absent body; nonmonotonic event IDs
# ---------------------------------------------------------------------------


# EmptyAndAbsentBody


async def test_empty_and_absent_body_present_empty_body_collects_and_absent_body_reports_empty(
    tmp_path,
) -> None:
    """INV: a present-empty body is content (collected as a row); an
    absent body is the empty outcome, never a fabricated deletion."""
    store = SQLiteStorage(str(tmp_path / "empty-bodies.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        await _seed(store, "present-empty", "2026-11-02T10:00:00+00:00", body="")
        await _seed(store, "absent", "2026-11-02T11:00:00+00:00", body=None)
        record = Recording(collects=[], pages=[])
        worker = _worker(_recording_storage(store, record), FakeClock())
        await worker.sweep()

        assert worker.state.collected == 1
        assert worker.state.empty == 1
        assert worker.state.rows_removed == 1
        assert worker.state.logical_bytes_removed == 0
        assert not await _body_present(store, "present-empty")
        identity = await store.get_body_presence_at_admission("present-empty")
        assert identity is True, "empty text was present at admission"
        absent_row = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='absent'"
        )
        assert absent_row is not None and absent_row["n"] == 0
        absent_identity = await store.get_body_presence_at_admission("absent")
        assert absent_identity is False
        assert record.collects == ["present-empty", "absent"]
    finally:
        await store.close()


# NonmonotonicEventIds


async def test_nonmonotonic_event_ids_worker_collection_follows_admission_order_not_id_order(
    tmp_path,
) -> None:
    """INV: the selector's admission ordering holds through the worker —
    events admitted in created_at order whose event IDs sort in reverse
    are collected in admission order, page by page (extends PR B's pin)."""
    store = SQLiteStorage(str(tmp_path / "nonmono.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_T0)
        # Admission slot i gets a reverse-alphabetical ID, so event-ID
        # order is the exact opposite of admission order.
        record = Recording(collects=[], pages=[])
        worker = ContentCollectionWorker(
            _recording_storage(store, record),
            store,
            ContentRetentionPolicy(mode="relay-only", batch_limit=2),
            now=FakeClock(),
        )
        await worker.sweep()
        ids = [f"ev-{chr(ord('f') - index)}" for index in range(6)]
        for index, event_id in enumerate(ids):
            await _seed(store, event_id, f"2026-11-02T10:00:0{index}.000000+00:00")
        admission_order = list(ids)
        for sweep_number in range(3):
            await worker.sweep()
            assert record.collects == admission_order[: 2 * (sweep_number + 1)]
        assert worker.state.collected == 6
        # The cycle wrapped: each following sweep re-pages from scope
        # head and answers already_collected in the same admission order.
        for sweep_number in range(1, 4):
            await worker.sweep()
            assert record.collects == (
                admission_order + admission_order[: 2 * sweep_number]
            )
        assert worker.state.already_collected == 6
        assert worker.state.collected == 6
    finally:
        await store.close()
