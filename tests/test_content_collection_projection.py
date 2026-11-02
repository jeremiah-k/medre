"""Adapter-independent aggregate collection-worker projection contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.storage.content_retention import (
    CandidatePage,
    ContentRetentionPolicy,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.collection import (
    ContentCollectionWorker,
    ContentCollectionWorkerError,
)
from medre.runtime.snapshot import build_runtime_snapshot
from tests.helpers.snapshot import make_fake_app


@pytest.fixture()
def tmp_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """A MedrePaths rooted at a temp directory (isolated MEDRE_HOME)."""
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Projection surfaces: diagnostic_snapshot and build_runtime_snapshot
# ---------------------------------------------------------------------------


class _NullCandidateSource:
    """Empty candidate source for projection-only worker construction."""

    async def page_collection_candidates(
        self,
        *,
        after: tuple[str, str] | None,
        limit: int,
        watermark: str | None,
    ) -> CandidatePage:
        return CandidatePage(event_ids=[], next_after=None, has_more=False)


def _worker_with_state(**overrides: Any) -> ContentCollectionWorker:
    """A real worker (inert storage) whose runtime state is scripted."""
    worker = ContentCollectionWorker(
        SQLiteStorage(":memory:"),
        _NullCandidateSource(),
        ContentRetentionPolicy(mode="relay-only"),
    )
    for name, value in overrides.items():
        setattr(worker.state, name, value)
    return worker


def _bare_app(worker: ContentCollectionWorker | None) -> MedreApp:
    """A bare MedreApp carrying only what diagnostic_snapshot reads."""
    app = object.__new__(MedreApp)
    app._collection_worker = worker
    app._capacity_controller = None
    app._ingress_worker = None
    app._state = RuntimeState.RUNNING
    app.config = SimpleNamespace(  # type: ignore[assignment]
        limits=SimpleNamespace(shutdown_drain_timeout_seconds=10)
    )
    return app


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


# DiagnosticProjection


def test_diagnostic_projection_block_is_zeroed_when_worker_absent() -> None:
    block = _bare_app(None).diagnostic_snapshot()["content_collection"]
    assert block == _ZEROED


def test_diagnostic_projection_block_reflects_worker_state() -> None:
    worker = _worker_with_state(
        restricted=True,
        awaiting_activation=True,
        degraded=True,
        collected=3,
        blocked=1,
        logical_bytes_removed=42,
        epoch=2,
        last_sweep_at="2026-11-01T00:00:00+00:00",
    )
    block = _bare_app(worker).diagnostic_snapshot()["content_collection"]
    assert block["worker_running"] is False
    assert block["enabled"] is True
    assert block["restricted"] is True
    assert block["awaiting_activation"] is True
    assert block["degraded"] is True
    assert block["collected"] == 3
    assert block["blocked"] == 1
    assert block["logical_bytes_removed"] == 42
    assert block["epoch"] == 2
    assert block["last_sweep_at"] == "2026-11-01T00:00:00+00:00"


def test_diagnostic_projection_block_carries_no_event_ids_or_error_entries() -> None:
    worker = _worker_with_state()
    worker.state.recent_errors.append(
        ContentCollectionWorkerError(
            timestamp="2026-11-01T00:00:00+00:00",
            kind="transient",
            event_id="evt-with-identity",
        )
    )
    block = _bare_app(worker).diagnostic_snapshot()["content_collection"]
    # The bounded error journal stays worker-local: the aggregate block
    # carries flags and counters only — no event IDs, no error entries.
    assert "evt-with-identity" not in json.dumps(block)
    assert json.dumps(block)  # JSON-safe


_SNAPSHOT_ZEROED = {
    "abandoned": False,
    "already_collected": 0,
    "awaiting_activation": False,
    "blocked": 0,
    "collected": 0,
    "degraded": False,
    "empty": 0,
    "enabled": False,
    "epoch": None,
    "events_examined": 0,
    "failed": 0,
    "last_sweep_at": None,
    "live_refresh": False,
    "logical_bytes_removed": 0,
    "restricted": False,
    "rows_removed": 0,
    "running": False,
    "scope": "process_local",
}


# RuntimeSnapshotProjection


def test_runtime_snapshot_projection_snapshot_defaults_for_app_without_worker() -> None:
    snapshot = build_runtime_snapshot(
        make_fake_app(),
        now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        monotonic_fn=lambda: 0.0,
    )
    assert snapshot["content_collection"] == _SNAPSHOT_ZEROED


def test_runtime_snapshot_projection_snapshot_projects_worker_state_defensively() -> (
    None
):
    fake_app = make_fake_app()
    fake_app.content_collection_state = _worker_with_state(
        collected=2, epoch=7, degraded=True
    ).state
    snapshot = build_runtime_snapshot(
        fake_app,
        now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        monotonic_fn=lambda: 0.0,
    )
    block = snapshot["content_collection"]
    assert block["collected"] == 2
    assert block["epoch"] == 7
    assert block["degraded"] is True
    assert block["scope"] == "process_local"


def test_runtime_snapshot_projection_snapshot_top_level_keys_stay_sorted() -> None:
    snapshot = build_runtime_snapshot(
        make_fake_app(),
        now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        monotonic_fn=lambda: 0.0,
    )
    keys = list(snapshot.keys())
    assert keys == sorted(keys)
    assert "content_collection" in keys


def test_runtime_snapshot_projection_snapshot_block_is_json_safe() -> None:
    snapshot = build_runtime_snapshot(
        make_fake_app(),
        now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        monotonic_fn=lambda: 0.0,
    )
    assert json.dumps(snapshot["content_collection"])


@pytest.mark.parametrize(
    ("missing", "default"),
    [
        ("abandoned", False),
        ("awaiting_activation", False),
        ("degraded", False),
        ("enabled", False),
        ("epoch", None),
        ("failed", 0),
        ("last_sweep_at", None),
        ("logical_bytes_removed", 0),
        ("restricted", False),
        ("rows_removed", 0),
        ("running", False),
    ],
)
def test_snapshot_projection_is_defensive_per_field(missing: str, default: Any) -> None:
    """A state object missing a field degrades to the zeroed default."""

    class _PartialState:
        pass

    fake_app = make_fake_app()
    fake_app.content_collection_state = _PartialState()
    snapshot = build_runtime_snapshot(
        fake_app,
        now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        monotonic_fn=lambda: 0.0,
    )
    assert snapshot["content_collection"][missing] == default


@pytest.mark.parametrize("surface", ["diagnostics", "runtime"])
def test_projection_matches_its_closed_published_schema(surface):
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/content-collection-state.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    schema = {**schema, "$ref": f"#/$defs/{surface}"}
    if surface == "diagnostics":
        block = _bare_app(
            _worker_with_state(collected=3, epoch=2)
        ).diagnostic_snapshot()["content_collection"]
    else:
        app = make_fake_app()
        app.content_collection_state = _worker_with_state(collected=3, epoch=2).state
        block = build_runtime_snapshot(app)["content_collection"]
    validator = Draft202012Validator(schema)
    validator.validate(block)
    assert list(validator.iter_errors({**block, "event_id": "private"}))
