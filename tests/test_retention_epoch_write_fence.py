"""Atomic retention-epoch fencing against operator reactivation and restarts.

The candidate page is advisory. The current durable activation must still own
that event under the collector's write reservation when its body is deleted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.core.storage.backend import ContentCollectionEpochChanged
from medre.core.storage.content_retention import ContentRetentionPolicy
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.collection import ContentCollectionWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.content_lifecycle import (
    FakeClock,
    body_present,
    seed_event,
)

_T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


async def test_epoch_rotated_after_candidate_read_blocks_old_write(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "fenced.sqlite"))
    await store.initialize()
    try:
        first = await store.open_retention_epoch(activated_by="operator", now=_T0)
        await seed_event(store, "before-rotation", "2026-11-02T10:00:00+00:00")
        page = await store.page_collection_candidates(
            after=None, limit=10, watermark=first.admission_watermark
        )
        assert page.event_ids == ["before-rotation"]
        next_epoch = await store.open_retention_epoch(
            activated_by="operator", now=_T0 + timedelta(minutes=1)
        )
        assert next_epoch.epoch == first.epoch + 1
        with pytest.raises(ContentCollectionEpochChanged):
            await store.collect_event_content(
                "before-rotation", dry_run=False, retention_epoch=first.epoch
            )
        with pytest.raises(ContentCollectionEpochChanged):
            await store.collect_event_content(
                "before-rotation", dry_run=False, retention_epoch=next_epoch.epoch
            )
        assert await body_present(store, "before-rotation")
        assert await store._read_one(
            "SELECT COUNT(*) AS count FROM event_content_collection_log"
        ) == {"count": 0}

        await seed_event(store, "after-rotation", "2026-11-03T10:00:00+00:00")
        result = await store.collect_event_content(
            "after-rotation", dry_run=False, retention_epoch=next_epoch.epoch
        )
        assert result["status"] == "collected"
        assert not await body_present(store, "after-rotation")
        assert await body_present(store, "before-rotation")
    finally:
        await store.close()


async def test_heartbeat_cannot_revive_a_superseded_epoch(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "heartbeat.sqlite"))
    await store.initialize()
    try:
        old = await store.open_retention_epoch(activated_by="operator", now=_T0)
        new = await store.open_retention_epoch(
            activated_by="operator", now=_T0 + timedelta(minutes=1)
        )
        with pytest.raises(ContentCollectionEpochChanged):
            await store.observe_retention_heartbeat(
                epoch=old.epoch, now=_T0 + timedelta(minutes=2)
            )
        current = await store.latest_retention_epoch()
        assert current is not None
        assert current.epoch == new.epoch
        assert current.last_observed_active_at is None
    finally:
        await store.close()


@pytest.mark.parametrize("start_loop", [False, True])
async def test_new_live_worker_reopens_fresh_epoch_to_exclude_unknown_gap(
    tmp_path,
    start_loop,
) -> None:
    store = SQLiteStorage(str(tmp_path / "restart.sqlite"))
    await store.initialize()
    clock = FakeClock(_T0)
    worker = None
    try:
        old = await store.open_retention_epoch(activated_by="operator", now=_T0)
        await store.observe_retention_heartbeat(epoch=old.epoch, now=_T0)
        # These events could have been admitted under an intervening retain
        # configuration, not the previously running relay-only worker.
        await seed_event(store, "unknown-policy-gap", "2026-11-02T10:00:00+00:00")
        worker = ContentCollectionWorker(
            store,
            store,
            ContentRetentionPolicy(mode="relay-only", interval_seconds=3600),
            now=clock,
        )
        if start_loop:
            await worker.start()
            assert await wait_until(
                lambda: worker.state.last_sweep_at is not None, timeout=5.0
            )
        else:
            await worker.sweep()
        current = await store.latest_retention_epoch()
        assert current is not None and current.epoch == old.epoch + 1
        assert current.admission_watermark == "2026-11-02T10:00:00+00:00"
        # Positive control proves collection ran under the new boundary.
        await seed_event(store, "post-boundary", "2026-11-03T10:00:00+00:00")
        clock.value += timedelta(seconds=3600)
        await worker.sweep()
        assert not await body_present(store, "post-boundary")
        assert await body_present(store, "unknown-policy-gap")
        assert worker.state.collected == 1
    finally:
        if worker is not None:
            await worker.stop()
        await store.close()


async def test_retain_policy_cannot_be_bypassed_by_direct_sweep(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "retain-sweep.sqlite"))
    await store.initialize()
    try:
        await seed_event(store, "still-retained", "2026-11-02T10:00:00+00:00")
        worker = ContentCollectionWorker(
            store,
            store,
            ContentRetentionPolicy(mode="retain"),
            now=FakeClock(_T0),
        )
        await worker.sweep()
        assert await store.latest_retention_epoch() is None
        assert await body_present(store, "still-retained")
        assert worker.state.events_examined == 0
    finally:
        await store.close()
