# medre-test-tier: compatibility
"""Runtime wiring for the policy-governed content collection worker.

Covers the app-level and snapshot projections of the runtime-local
worker state (this file's first section) and the full-runtime lifecycle
behavior — construction gates, first-start activation epoch, end-to-end
collection through the real candidate selector, and bounded shutdown
(the sections below).

The worker's own unit tests live in tests/test_content_collection_worker.py;
the storage epoch/selector/collector contract lives in
tests/test_content_retention_policy.py and tests/test_content_collection.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

import msgspec
import pytest

from medre.config.model import (
    ContentRetentionConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.storage.content_retention import (
    RetentionEpoch,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.app import MedreApp, RuntimeState, StartupScope
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import build_runtime_snapshot
from tests.helpers.async_utils import wait_until
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
)
from tests.helpers.storage import make_storage_event


@pytest.fixture()
def tmp_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """A MedrePaths rooted at a temp directory (isolated MEDRE_HOME)."""
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


_ZEROED = {
    "worker_running": False,
    "enabled": False,
    "abandoned": False,
    "restricted": False,
    "awaiting_activation": False,
    "degraded": False,
    "last_sweep_at": None,
    "epoch": None,
    "events_examined": 0,
    "collected": 0,
    "already_collected": 0,
    "blocked": 0,
    "empty": 0,
    "failed": 0,
    "rows_removed": 0,
    "logical_bytes_removed": 0,
}

# ---------------------------------------------------------------------------
# Full-runtime wiring: construction gates and LIVE lifecycle
# ---------------------------------------------------------------------------

_RELAY_ONLY = ContentRetentionConfig(
    enabled=True,
    mode="relay-only",
    interval_seconds=1,
    batch_limit=10,
)

_RETAIN = ContentRetentionConfig(enabled=True, mode="retain")


def _config(
    content_retention: ContentRetentionConfig,
    *,
    shutdown_timeout: int = 5,
) -> RuntimeConfig:
    base = make_multi_adapter_config()
    return dataclasses.replace(
        base,
        content_retention=content_retention,
        runtime=RuntimeOptions(
            name="collection-wiring", shutdown_timeout_seconds=shutdown_timeout
        ),
    )


async def _seed_eligible(store: SQLiteStorage, event_id: str, body: str) -> None:
    """Seed one settled ordinary text event admitted right now.

    Controlled settlement fixture: the append owner is released so the
    guarded collector deems the event eligible, exactly like the
    retention-policy tests.
    """
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": body}, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        ("2026-10-31T01:00:00+00:00", event_id),
    )


async def _seed_pending_sibling(store: SQLiteStorage, event_id: str, body: str) -> None:
    """Seed one admitted event whose owning lifecycle has not settled.

    The unsettled mandatory owner is the pending-sibling blocker family:
    the candidate selector pages the event, and the guarded collector
    refuses it as blocked — nothing is collected.
    """
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": body}, metadata={}
    )
    await store.append(event)


async def _body_present(store: SQLiteStorage, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT body FROM event_body_content WHERE event_id=?", (event_id,)
    )
    return row is not None


async def _wait_for_epoch(app: MedreApp) -> RetentionEpoch:
    """Wait for the worker's first sweep to open the activation epoch."""
    observed: list[RetentionEpoch] = []

    async def _read() -> bool:
        assert app.storage is not None
        epoch = await app.storage.latest_retention_epoch()
        if epoch is not None:
            observed.append(epoch)
            return True
        return False

    assert await wait_until(_read, timeout=5.0)
    return observed[-1]


# LiveWiring


async def test_live_wiring_enabled_relay_only_collects_settled_event_end_to_end(
    tmp_paths: MedrePaths,
) -> None:
    config = dataclasses.replace(
        _config(_RELAY_ONLY),
        storage=StorageConfig(backend="sqlite", path=str(tmp_paths.database_path)),
    )
    tmp_paths.database_path.parent.mkdir(parents=True, exist_ok=True)
    seeded = SQLiteStorage(str(tmp_paths.database_path))
    await seeded.initialize()
    try:
        await _seed_eligible(seeded, "historic", "DO-NOT-DELETE-historic")
    finally:
        await seeded.close()
    app = await build_and_start(config, tmp_paths)
    try:
        worker = app._collection_worker
        assert worker is not None
        assert app.storage is not None

        # Pre-activation history is pinned out by the first-start
        # watermark.
        epoch = await _wait_for_epoch(app)
        assert epoch.activated_by == "runtime-first-start"
        assert epoch.admission_watermark is not None

        # Post-activation candidates: one settled eligible event and
        # one pending sibling whose owner has not settled.
        await _seed_eligible(app.storage, "fresh", "SECRET-BODY-fresh")
        await _seed_pending_sibling(app.storage, "pending", "SECRET-BODY-pending")

        async def _collected() -> bool:
            return not await _body_present(app.storage, "fresh")

        assert await wait_until(_collected, timeout=10.0)

        receipt = await app.storage._read_one(
            "SELECT event_id, policy, rows_removed FROM "
            "event_content_collection_log WHERE event_id='fresh'"
        )
        assert receipt is not None
        assert receipt["policy"] == "relay-only"
        assert receipt["rows_removed"] == 1
        assert worker.state.collected >= 1
        # The pending sibling is paged and refused, never collected.
        assert await wait_until(lambda: worker.state.blocked >= 1, timeout=10.0)
        assert not await _body_present(app.storage, "fresh")
        # The pending sibling is paged and refused, never collected.
        assert await _body_present(app.storage, "pending")
        # Pre-activation history was never a candidate.
        assert await _body_present(app.storage, "historic")

        block = app.diagnostic_snapshot()["content_collection"]
        assert block["worker_running"] is True
        assert block["collected"] >= 1
        assert block["epoch"] == epoch.epoch
        snapshot = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snapshot["content_collection"]["collected"] >= 1
        assert "SECRET-BODY" not in json.dumps(snapshot["content_collection"])
    finally:
        await clean_stop(app)
    assert app.state is RuntimeState.STOPPED
    assert app._collection_worker is not None
    assert app._collection_worker.state.abandoned is False
    assert app._collection_worker._task is None


async def test_live_wiring_disabled_config_constructs_no_worker_and_no_epoch(
    tmp_paths: MedrePaths,
) -> None:
    config = _config(ContentRetentionConfig())
    app = await build_and_start(config, tmp_paths)
    try:
        assert app._collection_worker is None
        assert app.content_collection_state.enabled is False
        assert app.storage is not None
        assert await app.storage.latest_retention_epoch() is None
        block = app.diagnostic_snapshot()["content_collection"]
        assert block == _ZEROED
    finally:
        await clean_stop(app)
    assert app.state is RuntimeState.STOPPED


async def test_live_wiring_retain_mode_constructs_no_worker(
    tmp_paths: MedrePaths,
) -> None:
    config = _config(_RETAIN)
    app = await build_and_start(config, tmp_paths)
    try:
        assert app._collection_worker is None
        assert app.storage is not None
        assert await app.storage.latest_retention_epoch() is None
    finally:
        await clean_stop(app)
    assert app.state is RuntimeState.STOPPED


async def test_live_wiring_replay_scope_never_constructs_worker(
    tmp_paths: MedrePaths,
) -> None:
    config = _config(_RELAY_ONLY)
    app = RuntimeBuilder(config, tmp_paths).build()
    await app.start(scope=StartupScope.REPLAY)
    try:
        assert app._collection_worker is None
        assert app.storage is not None
        assert await app.storage.latest_retention_epoch() is None
        assert app.diagnostic_snapshot()["content_collection"]["enabled"] is False
    finally:
        await app.stop()
    assert app.state is RuntimeState.STOPPED


async def test_live_wiring_stop_with_worker_mid_sweep_ends_cleanly(
    tmp_paths: MedrePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection transaction in flight at stop() ends bounded.

    A gate on the storage collector holds the worker mid-sweep; stop()
    must still finish bounded (stage-2 force cancel), the runtime must
    reach STOPPED with storage closed, and no task may linger once the
    gate releases.
    """
    config = _config(_RELAY_ONLY, shutdown_timeout=1)
    app = await build_and_start(config, tmp_paths)
    try:
        worker = app._collection_worker
        assert worker is not None
        assert app.storage is not None
        await _wait_for_epoch(app)
        await _seed_eligible(app.storage, "target", "SECRET-BODY-target")

        # Deterministic mid-sweep hook: gate the FIRST collector call.
        # The worker holds the same storage object, so the instance
        # attribute patch intercepts exactly its sweep.
        original = app.storage.collect_event_content
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def gated(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            entered.set()
            await gate.wait()
            return await original(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )

        monkeypatch.setattr(app.storage, "collect_event_content", gated)
        assert await wait_until(lambda: entered.is_set(), timeout=10.0)

        tasks_before = asyncio.all_tasks()
        await app.stop()

        assert app.state is RuntimeState.STOPPED
        assert worker._task is None
        assert worker.state.running is False
        assert worker.state.abandoned is False
        assert worker.has_retained_work is False
        gate.set()  # release the hook; nothing may still be waiting
        await wait_until(
            lambda: all(
                task.done() for task in asyncio.all_tasks() if task not in tasks_before
            ),
            timeout=2.0,
        )
    finally:
        gate.set()
        if app.state is RuntimeState.STOPPING:
            await app.stop()
    # The stopped runtime stays stopped; a second stop is a no-op.
    await app.stop()
    assert app.state is RuntimeState.STOPPED
