"""Replay bridge-condition tests: live delivery vs replay delivery traceability.

Tests verify that BEST_EFFORT replay of events previously delivered via a
live bridge session produces distinguishable receipts with correct source
and replay_run_id fields.  Replay is NOT dedupe — each replay run creates
new receipt rows per design.

Uses the same RuntimeBuilder / fake-adapter infrastructure as
test_replay_pipeline_integration.py, exercising the full
ingress → route → plan → render → deliver path for live events, then
replaying via ReplayEngine.

Tests
-----
* test_replay_after_fake_bridge — live receipt vs replay receipt distinguishable
* test_replay_receipt_distinguishable — two replay runs produce distinct run_ids
* test_duplicate_send_caveat_reflected — replay is not dedupe (3 receipts for 1 event)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.core.events.canonical import CanonicalEvent, EventMetadata
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    collect_replay_summary,
)
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-001",
    source_adapter: str = "main",
) -> CanonicalEvent:
    """Minimal canonical event for seeding storage and bridge ingress."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="fake-transport",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello bridge"},
        metadata=EventMetadata(),
    )


def _make_paths(tmp: Path) -> MedrePaths:
    """MedrePaths pointing at *tmp* directories."""
    return MedrePaths(
        config_dir=tmp / "config",
        config_file=tmp / "config" / "config.toml",
        state_dir=tmp / "state",
        data_dir=tmp / "data",
        cache_dir=tmp / "cache",
        log_dir=tmp / "logs",
        database_path=tmp / "state" / "medre.sqlite",
    )


def _make_config() -> RuntimeConfig:
    """Two fake Matrix adapters: main → secondary (route_a)."""
    return RuntimeConfig(
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="main",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
                "secondary": MatrixRuntimeConfig(
                    adapter_id="secondary",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="route_a",
                    source_adapters=("main",),
                    dest_adapters=("secondary",),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
async def bridge_env(tmp_path: Path):
    """Build a fully wired runtime with fake adapters for bridge + replay.

    Yields a namespace with:
        app          – MedreApp (started)
        storage      – SQLiteStorage
        replay       – ReplayEngine
        seed_live    – helper: inject event via handle_ingress, return (event, outcomes)
    """
    paths = _make_paths(tmp_path)
    config = _make_config()

    for d in (
        paths.state_dir,
        paths.data_dir,
        paths.cache_dir,
        paths.log_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)

    app = RuntimeBuilder(config, paths).build()
    await app.start()

    storage = app.storage
    pipeline = app.pipeline_runner

    replay = ReplayEngine(
        storage=storage,
        pipeline=pipeline,
        event_bus=app.event_bus,
        diagnostician=app.diagnostician,
    )

    class Env:
        pass

    env = Env()
    env.app = app
    env.storage = storage
    env.replay = replay
    env.pipeline = pipeline

    async def seed_live(
        event: CanonicalEvent | None = None,
    ) -> tuple[CanonicalEvent, list[Any]]:
        """Inject event via handle_ingress (live path), return (event, outcomes)."""
        if event is None:
            event = _make_event()
        outcomes = await pipeline.handle_ingress(event)
        return event, outcomes

    env.seed_live = seed_live

    yield env

    await app.stop()


# ===================================================================
# Test 1: live delivery vs replay delivery are distinguishable
# ===================================================================


@pytest.mark.asyncio
async def test_replay_after_fake_bridge(bridge_env) -> None:
    """Live delivery creates source='live' receipt; replay creates source='replay'.

    1. Inject event via handle_ingress → live delivery receipt (source='live')
    2. Replay same event via ReplayEngine BEST_EFFORT → replay receipt
    3. Verify: 2 receipts total, different source values
    4. Verify: replay receipt has source='replay' and replay_run_id populated
    """
    env = bridge_env
    event, outcomes = await env.seed_live()
    event_id = event.event_id

    # Live delivery should have succeeded.
    assert len(outcomes) >= 1, "Live delivery should produce at least one outcome"
    assert outcomes[0].status == "success"

    # Check live receipt in storage.
    live_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? AND source = ?",
        (event_id, "live"),
    )
    assert len(live_rows) >= 1, "At least one live receipt should exist"
    assert live_rows[0]["source"] == "live"

    # Now replay the same event via ReplayEngine.
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-bridge-001",
        correlation_ids=[event_id],
    )
    summary = await collect_replay_summary(env.replay.replay(request))
    assert summary.events_replayed >= 1

    # Check replay receipt in storage.
    replay_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? AND source = ?",
        (event_id, "replay"),
    )
    assert len(replay_rows) >= 1, "At least one replay receipt should exist"
    assert replay_rows[0]["source"] == "replay"
    assert replay_rows[0]["replay_run_id"] == "run-bridge-001"

    # Total: live + replay receipts.
    all_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ?",
        (event_id,),
    )
    assert (
        len(all_rows) >= 2
    ), f"Expected at least 2 receipts (1 live + 1 replay), got {len(all_rows)}"

    # Sources are distinct.
    sources = {row["source"] for row in all_rows}
    assert sources == {"live", "replay"}


# ===================================================================
# Test 2: two replay runs produce distinct replay_run_id
# ===================================================================


@pytest.mark.asyncio
async def test_replay_receipt_distinguishable(bridge_env) -> None:
    """Replaying the same event with different run_ids produces distinct receipts.

    Each replay run creates a new receipt with its own replay_run_id.
    Native refs from each replay are distinct (different receipt rows).
    """
    env = bridge_env
    event, _ = await env.seed_live()
    event_id = event.event_id

    # Replay #1 with run_id "run-alpha".
    request_alpha = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-alpha",
        correlation_ids=[event_id],
    )
    await collect_replay_summary(env.replay.replay(request_alpha))

    # Replay #2 with run_id "run-beta".
    request_beta = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="run-beta",
        correlation_ids=[event_id],
    )
    await collect_replay_summary(env.replay.replay(request_beta))

    # Verify: each run produced receipts with its own run_id.
    alpha_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? "
        "AND replay_run_id = ? AND source = 'replay'",
        (event_id, "run-alpha"),
    )
    beta_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? "
        "AND replay_run_id = ? AND source = 'replay'",
        (event_id, "run-beta"),
    )

    assert len(alpha_rows) >= 1, "run-alpha should have at least one replay receipt"
    assert len(beta_rows) >= 1, "run-beta should have at least one replay receipt"

    # Native refs from each replay are distinct (different receipt rows,
    # different sequence numbers).
    alpha_seqs = {row["sequence"] for row in alpha_rows}
    beta_seqs = {row["sequence"] for row in beta_rows}
    assert alpha_seqs.isdisjoint(
        beta_seqs
    ), "Receipt sequences from different replay runs must not overlap"


# ===================================================================
# Test 3: replay is NOT dedupe — duplicate-send caveat
# ===================================================================


@pytest.mark.asyncio
async def test_duplicate_send_caveat_reflected(bridge_env) -> None:
    """BEST_EFFORT replay creates new receipts each time (no dedup).

    CAVEAT: Each BEST_EFFORT replay of the same event produces additional
    receipt rows.  This is expected per design — replay does not deduplicate.
    Operators must use application-level dedup or run_id tracking.

    After 1 live delivery + 2 BEST_EFFORT replays: 3+ receipt rows total,
    each independently persisted.
    """
    env = bridge_env
    event, _ = await env.seed_live()
    event_id = event.event_id

    # Count live receipts.
    live_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ? AND source = ?",
        (event_id, "live"),
    )
    live_count = len(live_rows)
    assert live_count >= 1

    # Replay #1.
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="replay-1",
        correlation_ids=[event_id],
    )
    await collect_replay_summary(env.replay.replay(request))

    # Replay #2.
    request2 = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        run_id="replay-2",
        correlation_ids=[event_id],
    )
    await collect_replay_summary(env.replay.replay(request2))

    # Total receipts: live + replay1 + replay2.
    all_rows = await env.storage._read_all(
        "SELECT * FROM delivery_receipts WHERE event_id = ?",
        (event_id,),
    )

    # No dedup: each replay adds new receipts.
    # live_count (live) + at least 1 per replay run = at least 3.
    expected_min = live_count + 2
    assert len(all_rows) >= expected_min, (
        f"Expected at least {expected_min} receipts "
        f"(1 live + 1 replay-1 + 1 replay-2), got {len(all_rows)}. "
        "Replay is not dedupe — duplicate-send risk remains."
    )

    # Verify that replay_run_id is populated and distinct per run.
    replay_rows = [r for r in all_rows if r["source"] == "replay"]
    run_ids = {r["replay_run_id"] for r in replay_rows}
    assert "replay-1" in run_ids
    assert "replay-2" in run_ids

    # Live receipts remain untouched (source='live', no replay_run_id).
    live_after = [r for r in all_rows if r["source"] == "live"]
    for row in live_after:
        assert (
            row["replay_run_id"] is None
        ), "Live receipts should not have replay_run_id set"
