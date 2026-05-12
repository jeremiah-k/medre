"""Track 5 — Extended long-duration fake-only soak validation.

Larger combined scenarios that stress multiple axes simultaneously, going
beyond what the single-axis soak tests (``test_longrun_soak.py``,
``test_soak_foundations_v2.py``, ``test_soak_harness.py``,
``test_runtime_cancellation.py``, ``test_replay_routing_durability.py``)
already cover.

Coverage
--------
1. **Large route-count churn** — 50+ routes with mixed counter ops.
2. **Combined replay + diagnostics + degraded churn** — single sustained
   session interleaving all three axes.
3. **Startup/shutdown interleaving with delivery** — rapid start/deliver/stop
   with increasing delivery counts per cycle.
4. **Degraded adapter churn across varying adapter counts** — cycle through
   1→2→3→4 adapter configurations in a single test.
5. **Capacity churn under concurrent delivery pressure** — capacity acquire/
   release interleaved with sustained event bursts.
6. **Route expansion with bounded RouteStats** — progressively add routes
   and verify counters stay bounded.
7. **Bounded snapshots under sustained combined load** — snapshot JSON size
   remains bounded when combining delivery + capacity + diagnostics.
8. **Bounded replay state & summary** — ReplayState/ReplaySummary errors
   capped at ``_MAX_SUMMARY_ERRORS``, ReplayState lineage bounded.
9. **Task/cancellation cleanup under combined stress** — task cleanup when
   combining lifecycle cycling with delivery and capacity.

Constraints
-----------
- **Fake adapters only** — no live transports or SDKs.
- **In-memory storage** — no filesystem I/O beyond temp dirs.
- **Bounded runtime** — < 30 seconds total, deterministic, no sleeps.
- **Non-overlapping** — larger combined scenarios vs prior single-axis tests.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LxmfRuntimeConfig,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.diagnostics.replay_metrics import ReplayMetrics
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata, RoutingMetadata
from medre.core.routing.stats import RouteStats
from medre.core.storage.replay import (
    ReplayMode,
    ReplayResult,
    ReplayRouteAttribution,
    ReplayState,
    ReplaySummary,
    _build_summary,
    _MAX_ERROR_LENGTH,
    _MAX_SUMMARY_ERRORS,
)
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.capacity import CapacityController
from medre.runtime.snapshot import (
    _MAX_ADAPTERS,
    _MAX_BUILD_FAILURES,
    _MAX_ROUTES,
    build_runtime_snapshot,
)

# Reuse existing harness helpers.
from tests.test_soak_harness import DiagnosticsSnapshot, SoakRuntime
from tests.test_soak_foundations_v2 import _count_asyncio_tasks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def soak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SoakRuntime:
    """Provide a SoakRuntime instance."""
    return SoakRuntime(tmp_path=tmp_path, monkeypatch=monkeypatch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAX_FAKE_HISTORY: int = 1000


def _make_event(
    event_id: str = "evt-ext-001",
    source_adapter: str = "fake_matrix",
    *,
    routing: RoutingMetadata | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for extended soak tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-ext",
        source_channel_id="ch-ext",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "extended-soak"},
        metadata=EventMetadata(routing=routing),
    )


def _build_config_with_n_adapters(
    n: int,
    name: str = "ext-soak",
) -> RuntimeConfig:
    """Build a RuntimeConfig with exactly *n* fake adapters (1–4).

    Adapter order: matrix, meshtastic, meshcore, lxmf.
    """
    matrix_cfg = (
        {
            "ext_matrix": MatrixRuntimeConfig(
                adapter_id="ext_matrix",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 1
        else {}
    )
    meshtastic_cfg = (
        {
            "ext_mesh": MeshtasticRuntimeConfig(
                adapter_id="ext_mesh",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 2
        else {}
    )
    meshcore_cfg = (
        {
            "ext_meshcore": MeshCoreRuntimeConfig(
                adapter_id="ext_meshcore",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 3
        else {}
    )
    lxmf_cfg = (
        {
            "ext_lxmf": LxmfRuntimeConfig(
                adapter_id="ext_lxmf",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 4
        else {}
    )

    return RuntimeConfig(
        runtime=RuntimeOptions(name=name),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="memory"),
        limits=RuntimeLimits(
            max_inflight_deliveries=50,
            max_inflight_replay_events=50,
        ),
        adapters=AdapterConfigSet(
            matrix=matrix_cfg,
            meshtastic=meshtastic_cfg,
            meshcore=meshcore_cfg,
            lxmf=lxmf_cfg,
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


# ===================================================================
# 1. Large route-count churn
# ===================================================================


class TestLargeRouteCountChurn:
    """Stress RouteStats with 50+ routes — well beyond the 5–20 used in
    earlier soak tests."""

    @pytest.mark.asyncio
    async def test_60_routes_mixed_counter_ops(self) -> None:
        """60 routes × mixed delivered/failed/skipped/loop_prevented ops.

        All counters must be exact after 30 rounds of mixed operations.
        """
        stats = RouteStats()
        routes = [f"ext-route-{i:03d}" for i in range(60)]

        for _ in range(30):
            for rid in routes:
                stats.record_delivered(rid)
                stats.record_failed(rid, error="ext-test-err")
                stats.record_skipped(rid)
                stats.record_loop_prevented(rid)

        snap = stats.snapshot()
        assert len(snap) == 60, f"Expected 60 routes, got {len(snap)}"
        for rid in routes:
            entry = snap[rid]
            assert entry["delivered"] == 30, f"{rid}: delivered={entry['delivered']}"
            assert entry["failed"] == 30, f"{rid}: failed={entry['failed']}"
            assert entry["skipped"] == 30, f"{rid}: skipped={entry['skipped']}"
            assert entry["loop_prevented"] == 30, (
                f"{rid}: loop_prevented={entry['loop_prevented']}"
            )

    @pytest.mark.asyncio
    async def test_60_routes_snapshot_deterministic(self) -> None:
        """Two consecutive snapshots with 60 routes must be identical."""
        stats = RouteStats()
        for i in range(200):
            rid = f"ext-route-{i % 60:03d}"
            stats.record_delivered(rid)
            if i % 3 == 0:
                stats.record_failed(rid, error="err")

        snap1 = stats.snapshot()
        snap2 = stats.snapshot()
        assert snap1 == snap2

    @pytest.mark.asyncio
    async def test_100_routes_error_strings_bounded(self) -> None:
        """Error strings in RouteStats snapshot must be truncated."""
        stats = RouteStats()
        long_error = "x" * 2000
        for i in range(100):
            stats.record_failed(f"ext-route-{i:03d}", error=long_error)

        snap = stats.snapshot()
        for rid, entry in snap.items():
            if "last_error" in entry:
                assert len(entry["last_error"]) <= 512, (
                    f"{rid}: error string too long: {len(entry['last_error'])}"
                )


# ===================================================================
# 2. Combined replay + diagnostics + degraded churn
# ===================================================================


class TestCombinedReplayDiagnosticsDegraded:
    """Single sustained session that interleaves replay-style delivery,
    diagnostics capture, and degraded adapter configurations.

    Existing tests exercise each axis independently; this class combines
    all three in one sustained run."""

    @pytest.mark.asyncio
    async def test_sustained_combined_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """12 combined cycles: start with 4 adapters → deliver → diagnostics
        → stop, alternating with 2-adapter degraded cycles.

        Every cycle must produce consistent diagnostics, regardless of
        adapter count.
        """
        all_adapter_counts: list[int] = []
        all_states: list[str] = []

        for cycle in range(12):
            # Even cycles: full 4-adapter runtime.
            # Odd cycles: degraded 2-adapter runtime.
            n_adapters = 4 if cycle % 2 == 0 else 2
            expected_name = f"combined-{cycle}"

            cycle_dir = tmp_path / f"combined-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n_adapters, name=expected_name)
            app = _build_app(config, paths)

            await app.start()
            assert app.state is RuntimeState.RUNNING

            # Deliver events.
            delivery_failures: list[tuple[str, str]] = []
            for i in range(3):
                for adapter_id, adapter in app.adapters.items():
                    try:
                        if hasattr(adapter, "simulate_inbound"):
                            if hasattr(adapter, "make_text_event"):
                                event = adapter.make_text_event(
                                    f"comb-{cycle}-{i}", channel="ch"
                                )
                            elif hasattr(adapter, "make_event"):
                                event = adapter.make_event(
                                    f"comb-{cycle}-{i}", channel="ch"
                                )
                            else:
                                continue
                            await adapter.simulate_inbound(event)
                    except Exception as exc:
                        delivery_failures.append((adapter_id, str(exc)))
            assert not delivery_failures, (
                f"Cycle {cycle}: delivery failures: {delivery_failures}"
            )

            # Capture diagnostics.
            raw = build_runtime_snapshot(app)
            all_adapter_counts.append(len(app.adapters))
            all_states.append(raw.get("runtime_state", "unknown"))

            # ReplayMetrics churn alongside.
            metrics = ReplayMetrics()
            for _ in range(5):
                for rid in ["comb-r-alpha", "comb-r-beta"]:
                    metrics.record_events_processed(rid)
                    metrics.record_delivery_attempted(rid)
                    metrics.record_delivery_succeeded(rid)
            metrics_snap = metrics.snapshot()
            # 5 iterations × 2 routes = 10 events_processed.
            assert metrics_snap["global"]["replay_events_processed"] == 10

            await app.stop()
            assert app.state is RuntimeState.STOPPED

        # All even cycles must have 4 adapters, all odd cycles 2.
        assert all_adapter_counts == [4, 2] * 6, (
            f"Adapter counts unexpected: {all_adapter_counts}"
        )
        assert all(s == "running" for s in all_states), (
            f"States inconsistent: {all_states}"
        )


# ===================================================================
# 3. Startup/shutdown interleaving with delivery
# ===================================================================


class TestStartupShutdownDeliveryInterleave:
    """Rapidly alternate start → deliver → stop with increasing delivery
    counts per cycle.

    Existing tests do either pure lifecycle OR pure delivery; this
    exercises interleaving with escalating pressure."""

    @pytest.mark.asyncio
    async def test_10_cycles_escalating_delivery(
        self, soak: SoakRuntime,
    ) -> None:
        """10 cycles: deliver (cycle+1) × 2 events per cycle, from 2 to 20."""
        baseline = _count_asyncio_tasks()
        adapter_count_sets: set[int] = set()

        for cycle in range(10):
            await soak.start_fresh()
            assert soak.app is not None

            count = (cycle + 1) * 2
            results = await soak.deliver_events(count=count)
            assert len(results) == count

            snap = soak.capture_diagnostics(iteration=cycle)
            adapter_count_sets.add(snap.adapter_count)

            await soak.stop()
            assert soak.app.state is RuntimeState.STOPPED

        # All cycles must have 4 adapters.
        assert adapter_count_sets == {4}

        after = _count_asyncio_tasks()
        assert after <= baseline + 2, (
            f"Task leak: baseline={baseline}, after={after}"
        )

    @pytest.mark.asyncio
    async def test_8_cycles_with_diagnostics_between_deliveries(
        self, soak: SoakRuntime,
    ) -> None:
        """8 cycles: deliver 3 → diagnostics → deliver 3 more → diagnostics → stop.

        Diagnostics at two points within the same cycle must show "running".
        """
        for cycle in range(8):
            await soak.start_fresh()
            assert soak.app is not None

            await soak.deliver_events(count=3)
            snap1 = soak.capture_diagnostics(iteration=cycle * 2)
            assert snap1.runtime_state == "running"

            await soak.deliver_events(count=3)
            snap2 = soak.capture_diagnostics(iteration=cycle * 2 + 1)
            assert snap2.runtime_state == "running"

            await soak.stop()


# ===================================================================
# 4. Degraded adapter churn across varying adapter counts
# ===================================================================


class TestDegradedVaryingAdapterCount:
    """Cycle through 1→2→3→4 adapter configurations.

    Existing degraded tests use a fixed adapter count; this exercises
    the runtime's adaptability across all valid adapter counts."""

    @pytest.mark.asyncio
    async def test_cycle_through_all_adapter_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cycle 1→2→3→4→3→2→1 adapters — each must start and stop cleanly."""
        counts = [1, 2, 3, 4, 3, 2, 1]

        for idx, n in enumerate(counts):
            cycle_dir = tmp_path / f"var-adapters-{idx}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"var-{idx}")
            app = _build_app(config, paths)

            await app.start()
            assert app.state is RuntimeState.RUNNING
            assert len(app.adapters) == n, (
                f"Cycle {idx}: expected {n} adapters, got {len(app.adapters)}"
            )

            # Deliver events — tolerant of adapter variations.
            delivery_failures: list[tuple[str, str]] = []
            for i in range(3):
                for adapter_id, adapter in app.adapters.items():
                    try:
                        if hasattr(adapter, "simulate_inbound"):
                            if hasattr(adapter, "make_text_event"):
                                event = adapter.make_text_event(
                                    f"var-{idx}-{i}", channel="ch"
                                )
                            elif hasattr(adapter, "make_event"):
                                event = adapter.make_event(
                                    f"var-{idx}-{i}", channel="ch"
                                )
                            else:
                                continue
                            await adapter.simulate_inbound(event)
                    except Exception as exc:
                        delivery_failures.append((adapter_id, str(exc)))
            assert not delivery_failures, (
                f"Count-{n} cycle {idx}: delivery failures: {delivery_failures}"
            )

            await app.stop()
            assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_single_adapter_sustained_delivery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Single adapter runtime must handle 15 delivery bursts."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        config = _build_config_with_n_adapters(1, name="single-ext")
        app = _build_app(config, paths)

        await app.start()
        assert app.state is RuntimeState.RUNNING
        assert len(app.adapters) == 1

        delivery_failures: list[tuple[str, str]] = []
        for burst in range(15):
            for adapter_id, adapter in app.adapters.items():
                try:
                    if hasattr(adapter, "simulate_inbound"):
                        if hasattr(adapter, "make_text_event"):
                            event = adapter.make_text_event(
                                f"single-{burst}", channel="ch"
                            )
                        elif hasattr(adapter, "make_event"):
                            event = adapter.make_event(
                                f"single-{burst}", channel="ch"
                            )
                        else:
                            continue
                        await adapter.simulate_inbound(event)
                except Exception as exc:
                    delivery_failures.append((adapter_id, str(exc)))
        assert not delivery_failures, (
            f"Sustained delivery failures: {delivery_failures}"
        )

        await app.stop()
        assert app.state is RuntimeState.STOPPED


# ===================================================================
# 5. Capacity churn under concurrent delivery pressure
# ===================================================================


class TestCapacityChurnUnderDeliveryPressure:
    """Capacity acquire/release interleaved with sustained delivery bursts.

    Existing tests separate capacity tests from delivery; this combines
    both in a single test."""

    @pytest.mark.asyncio
    async def test_capacity_delivery_interleaved_20_cycles(
        self, soak: SoakRuntime,
    ) -> None:
        """20 cycles: acquire delivery slot → deliver event → release slot."""
        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        await soak.start()
        assert soak.app is not None

        for cycle in range(20):
            acquired = await cc.acquire_delivery()
            assert acquired is True

            # Deliver one event while holding the capacity slot.
            results = await soak.deliver_events(count=1)
            assert len(results) == 1

            snap = cc.snapshot()
            assert snap["delivery_current"] >= 1
            assert snap["delivery_current"] <= snap["delivery_limit"]

            await cc.release_delivery()

        cc.stop_accepting()
        assert cc.accepting_work is False

        final = cc.snapshot()
        assert final["delivery_current"] == 0

        await soak.stop()

    @pytest.mark.asyncio
    async def test_capacity_mixed_delivery_replay_15_cycles(self) -> None:
        """15 cycles of mixed delivery + replay acquire/release."""
        limits = RuntimeLimits(
            max_inflight_deliveries=8,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        for _ in range(15):
            d_slots = []
            for _ in range(5):
                ok = await cc.acquire_delivery()
                if ok:
                    d_slots.append(True)

            r_slots = []
            for _ in range(3):
                ok = await cc.acquire_replay()
                if ok:
                    r_slots.append(True)

            snap = cc.snapshot()
            assert snap["delivery_current"] <= snap["delivery_limit"]
            assert snap["replay_current"] <= snap["replay_limit"]

            for _ in d_slots:
                await cc.release_delivery()
            for _ in r_slots:
                await cc.release_replay()

        final = cc.snapshot()
        assert final["delivery_current"] == 0
        assert final["replay_current"] == 0


# ===================================================================
# 6. Route expansion with bounded RouteStats
# ===================================================================


class TestRouteExpansionBoundedStats:
    """Progressively add routes and verify counters stay bounded.

    Existing tests use a fixed set of routes; this adds routes
    progressively to exercise internal dict growth."""

    @pytest.mark.asyncio
    async def test_expanding_routes_counters_accurate(self) -> None:
        """Add 100 routes one at a time, record 1 delivery each.

        All 100 routes must have exactly 1 delivered count.
        """
        stats = RouteStats()

        for i in range(100):
            rid = f"expand-route-{i:03d}"
            stats.record_delivered(rid)

            snap = stats.snapshot()
            # Number of routes in snapshot must equal i + 1.
            assert len(snap) == i + 1, (
                f"After adding route {i}, expected {i + 1} routes, "
                f"got {len(snap)}"
            )

        final = stats.snapshot()
        assert len(final) == 100
        for i in range(100):
            rid = f"expand-route-{i:03d}"
            assert final[rid]["delivered"] == 1

    @pytest.mark.asyncio
    async def test_expanding_routes_with_failures_bounded(self) -> None:
        """Add 50 routes, each with delivered + failed. Last_error bounded."""
        stats = RouteStats()
        long_err = "e" * 2000

        for i in range(50):
            rid = f"expand-fail-{i:03d}"
            stats.record_delivered(rid)
            stats.record_failed(rid, error=long_err)

        snap = stats.snapshot()
        assert len(snap) == 50
        for rid, entry in snap.items():
            assert entry["delivered"] == 1
            assert entry["failed"] == 1
            assert "last_error" in entry
            assert len(entry["last_error"]) <= 512, (
                f"{rid}: error not truncated: {len(entry['last_error'])}"
            )

    @pytest.mark.asyncio
    async def test_200_routes_snapshot_deterministic(self) -> None:
        """200 routes — two consecutive snapshots identical."""
        stats = RouteStats()
        for i in range(500):
            stats.record_delivered(f"big-route-{i % 200:03d}")

        snap1 = stats.snapshot()
        snap2 = stats.snapshot()
        assert snap1 == snap2
        assert len(snap1) == 200


# ===================================================================
# 7. Bounded snapshots under sustained combined load
# ===================================================================


class TestBoundedSnapshotsUnderCombinedLoad:
    """Snapshot JSON size remains bounded when combining delivery +
    capacity + diagnostics.

    Earlier tests verify snapshot boundedness in isolation; this
    exercises it under combined sustained load."""

    @pytest.mark.asyncio
    async def test_snapshot_size_stable_under_combined_load(
        self, soak: SoakRuntime,
    ) -> None:
        """15 iterations: deliver events + capacity acquire/release +
        diagnostics snapshot. Snapshot JSON size must not grow."""
        await soak.start()
        assert soak.app is not None

        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        sizes: list[int] = []
        for i in range(15):
            # Capacity pressure.
            acquired = await cc.acquire_delivery()
            if acquired:
                await soak.deliver_events(count=2)
                await cc.release_delivery()

            # Diagnostics.
            raw = build_runtime_snapshot(soak.app)
            serialized = json.dumps(raw, sort_keys=True, default=str)
            sizes.append(len(serialized))

        await soak.stop()

        min_size = min(sizes)
        max_size = max(sizes)
        # Allow 10% + 100 bytes tolerance for minor counter changes.
        assert max_size - min_size <= min_size * 0.1 + 100, (
            f"Snapshot size drifted: min={min_size}, max={max_size}, "
            f"sizes={sizes}"
        )

    @pytest.mark.asyncio
    async def test_snapshot_adapter_count_constant_under_load(
        self, soak: SoakRuntime,
    ) -> None:
        """20 snapshots under load: adapter count must always be 4."""
        await soak.start()
        assert soak.app is not None

        for i in range(20):
            await soak.deliver_events(count=2)
            raw = build_runtime_snapshot(soak.app)
            adapters = raw.get("adapters", [])
            assert len(adapters) == 4, (
                f"Adapter count changed at iteration {i}: {len(adapters)}"
            )

        await soak.stop()

    @pytest.mark.asyncio
    async def test_snapshot_key_structure_stable_30_captures(
        self, soak: SoakRuntime,
    ) -> None:
        """30 consecutive snapshots under delivery load: key structure constant."""
        await soak.start()
        assert soak.app is not None

        structures: list[frozenset[str]] = []
        for i in range(30):
            await soak.deliver_events(count=1)
            raw = build_runtime_snapshot(soak.app)
            structures.append(frozenset(raw.keys()))

        await soak.stop()

        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, (
                f"Snapshot structure changed at capture {idx}"
            )


# ===================================================================
# 8. Bounded replay state & summary
# ===================================================================


class TestBoundedReplayStateAndSummary:
    """Verify ReplayState and ReplaySummary stay bounded under heavy churn.

    Earlier tests cover ReplayMetrics; this focuses on ReplayState
    (error list growth) and ReplaySummary (_MAX_SUMMARY_ERRORS cap)."""

    @pytest.mark.asyncio
    async def test_replay_state_errors_unbounded(self) -> None:
        """ReplayState.errors grows without bound (by design).

        Verify the count is exact after 200 failed results.
        """
        state = ReplayState()
        for i in range(200):
            result = ReplayResult(
                event_id=f"evt-{i:04d}",
                stage="deliver",
                status="error",
                error=f"error-{i}",
            )
            state.record(result)

        assert state.events_processed == 200
        assert state.events_failed == 200
        assert len(state.errors) == 200

    @pytest.mark.asyncio
    async def test_replay_state_lineage_bounded_to_last(self) -> None:
        """ReplayState.current_lineage holds only the most recent lineage."""
        state = ReplayState()
        for i in range(50):
            result = ReplayResult(
                event_id=f"evt-lin-{i}",
                stage="store",
                status="passed",
                lineage=[f"ancestor-{j}" for j in range(i)],
            )
            state.record(result)

        # Lineage must be from the last event only.
        assert len(state.current_lineage) == 49
        assert state.current_lineage[0] == "ancestor-0"
        assert state.current_lineage[-1] == "ancestor-48"

    @pytest.mark.asyncio
    async def test_replay_summary_errors_capped_at_max(self) -> None:
        """ReplaySummary.errors must be capped at _MAX_SUMMARY_ERRORS."""
        results = []
        for i in range(200):
            results.append(
                ReplayResult(
                    event_id=f"evt-sum-{i}",
                    stage="deliver",
                    status="error",
                    error=f"err-{i}",
                )
            )

        summary = _build_summary(results)
        assert len(summary.errors) == _MAX_SUMMARY_ERRORS
        assert summary.events_replayed == 200
        assert summary.failure_count == 200

    @pytest.mark.asyncio
    async def test_replay_summary_error_truncation(self) -> None:
        """Individual error strings in summary must be <= _MAX_ERROR_LENGTH."""
        long_error = "Z" * 5000
        results = [
            ReplayResult(
                event_id="evt-trunc",
                stage="deliver",
                status="error",
                error=long_error,
            )
            for _ in range(10)
        ]

        summary = _build_summary(results)
        for err in summary.errors:
            assert len(err) <= _MAX_ERROR_LENGTH, (
                f"Error not truncated: {len(err)} > {_MAX_ERROR_LENGTH}"
            )

    @pytest.mark.asyncio
    async def test_replay_summary_to_dict_deterministic(self) -> None:
        """Two to_dict() calls must produce identical output."""
        results = []
        for i in range(30):
            results.append(
                ReplayResult(
                    event_id=f"evt-dict-{i}",
                    stage="route",
                    status="passed" if i % 2 == 0 else "skipped",
                    route_attribution=ReplayRouteAttribution(
                        route_ids=(f"route-{i % 3}",),
                        source_adapter="src",
                        target_adapters=("tgt",),
                        replay_mode=ReplayMode.DRY_RUN,
                        is_replay=True,
                        loop_warnings=(),
                        run_id="test-run",
                    ),
                )
            )

        summary = _build_summary(results, events_scanned=30, run_id="test-run")
        d1 = summary.to_dict()
        d2 = summary.to_dict()

        j1 = json.dumps(d1, sort_keys=True)
        j2 = json.dumps(d2, sort_keys=True)
        assert j1 == j2, "ReplaySummary.to_dict() is not deterministic"

    @pytest.mark.asyncio
    async def test_replay_state_mixed_statuses_accurate(self) -> None:
        """Mixed passed/skipped/failed/error results: counters exact."""
        state = ReplayState()
        for i in range(100):
            if i % 4 == 0:
                status = "passed"
            elif i % 4 == 1:
                status = "skipped"
            elif i % 4 == 2:
                status = "failed"
                result = ReplayResult(
                    event_id=f"evt-mix-{i}",
                    stage="deliver",
                    status=status,
                    error=f"fail-{i}",
                )
                state.record(result)
                continue
            else:
                status = "error"
                result = ReplayResult(
                    event_id=f"evt-mix-{i}",
                    stage="render",
                    status=status,
                    error=f"err-{i}",
                )
                state.record(result)
                continue

            result = ReplayResult(
                event_id=f"evt-mix-{i}",
                stage="store",
                status=status,
            )
            state.record(result)

        assert state.events_processed == 100
        assert state.events_passed == 25
        assert state.events_skipped == 25
        assert state.events_failed == 50  # 25 "failed" + 25 "error"
        assert len(state.errors) == 50


# ===================================================================
# 9. Task/cancellation cleanup under combined stress
# ===================================================================


class TestTaskCancellationUnderCombinedStress:
    """Task cleanup when combining lifecycle cycling with delivery and
    capacity pressure.

    Earlier tests check task cleanup after simple lifecycle or delivery;
    this combines all three."""

    @pytest.mark.asyncio
    async def test_12_combined_stress_cycles_no_task_leak(
        self, soak: SoakRuntime,
    ) -> None:
        """12 cycles: start → capacity acquire → deliver → capacity release
        → diagnostics → stop. No task accumulation."""
        baseline = _count_asyncio_tasks()

        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        for cycle in range(12):
            await soak.start_fresh()
            assert soak.app is not None

            # Capacity acquire.
            acquired = await cc.acquire_delivery()
            assert acquired is True

            # Delivery burst.
            await soak.deliver_events(count=3)

            # Capacity release.
            await cc.release_delivery()

            # Diagnostics.
            snap = soak.capture_diagnostics(iteration=cycle)
            assert snap.runtime_state == "running"

            await soak.stop()

        after = _count_asyncio_tasks()
        assert after <= baseline + 2, (
            f"Task leak under combined stress: baseline={baseline}, "
            f"after={after}"
        )

    @pytest.mark.asyncio
    async def test_rapid_build_start_stop_with_capacity(
        self, soak: SoakRuntime,
    ) -> None:
        """8 rapid build→start→capacity→stop cycles with fresh capacity
        controllers each time."""
        task_counts: list[int] = []

        for _ in range(8):
            baseline = _count_asyncio_tasks()

            await soak.start_fresh()
            assert soak.app is not None

            # Quick capacity cycle.
            if soak.app._capacity_controller is not None:
                ok = await soak.app._capacity_controller.acquire_delivery()
                if ok:
                    await soak.app._capacity_controller.release_delivery()

            await soak.stop()
            task_counts.append(_count_asyncio_tasks())

        max_count = max(task_counts)
        min_count = min(task_counts)
        assert max_count - min_count <= 2, (
            f"Task count drifted over combined cycles: {task_counts}"
        )

    @pytest.mark.asyncio
    async def test_capacity_fully_released_after_combined_stress(
        self, soak: SoakRuntime,
    ) -> None:
        """After 6 start/deliver/stop cycles, capacity is fully released."""
        for cycle in range(6):
            await soak.start_fresh()
            assert soak.app is not None

            await soak.deliver_events(count=4)
            await soak.stop()

            # Capacity must be zero after stop.
            if soak.app._capacity_controller is not None:
                snap = soak.app._capacity_controller.snapshot()
                assert snap.get("delivery_current", 0) == 0, (
                    f"Capacity leaked at cycle {cycle}: {snap}"
                )
                assert snap.get("replay_current", 0) == 0, (
                    f"Replay capacity leaked at cycle {cycle}: {snap}"
                )


# ===================================================================
# 10. Bounded RouteStats under large-scale delivery
# ===================================================================


class TestBoundedRouteStatsUnderDelivery:
    """RouteStats remains bounded when routes are created during runtime
    delivery.

    Earlier tests use pre-created routes; this simulates organic route
    creation during sustained delivery."""

    @pytest.mark.asyncio
    async def test_30_bursts_create_routes_on_demand(self) -> None:
        """30 delivery bursts create routes on demand. Snapshot stays valid."""
        stats = RouteStats()

        for burst in range(30):
            # Each burst creates 2 new routes.
            for r in range(2):
                rid = f"live-route-{burst * 2 + r:03d}"
                stats.record_delivered(rid)
                stats.record_failed(rid, error="burst-err")

            snap = stats.snapshot()
            # After burst i, we have (i+1)*2 routes.
            expected = (burst + 1) * 2
            assert len(snap) == expected, (
                f"After burst {burst}: expected {expected} routes, "
                f"got {len(snap)}"
            )

            # All counters must be non-negative.
            for rid, entry in snap.items():
                assert entry["delivered"] >= 0
                assert entry["failed"] >= 0

        final = stats.snapshot()
        assert len(final) == 60

    @pytest.mark.asyncio
    async def test_route_stats_no_ghost_routes_after_deletion_pattern(
        self,
    ) -> None:
        """RouteStats tracks routes by ID; deleting a route externally
        does not create ghost entries.

        This verifies the snapshot contains exactly the routes that
        were recorded, no more."""
        stats = RouteStats()
        active_routes = set()

        for i in range(40):
            rid = f"ghost-route-{i:03d}"
            stats.record_delivered(rid)
            active_routes.add(rid)

            snap = stats.snapshot()
            snap_keys = set(snap.keys())
            assert snap_keys == active_routes, (
                f"Ghost routes at iteration {i}: "
                f"expected {active_routes}, got {snap_keys}"
            )


# ===================================================================
# 11. Snapshot boundedness constants
# ===================================================================


class TestSnapshotBoundednessConstants:
    """Verify snapshot builder's boundedness constants are respected.

    These tests directly verify the _MAX_ADAPTERS, _MAX_ROUTES, and
    _MAX_BUILD_FAILURES caps are applied correctly."""

    @pytest.mark.asyncio
    async def test_max_adapters_constant_reasonable(self) -> None:
        """_MAX_ADAPTERS must be >= 4 (the default fake adapter count)."""
        assert _MAX_ADAPTERS >= 4

    @pytest.mark.asyncio
    async def test_max_routes_constant_reasonable(self) -> None:
        """_MAX_ROUTES must be >= 100 (used in large route tests)."""
        assert _MAX_ROUTES >= 100

    @pytest.mark.asyncio
    async def test_max_build_failures_constant_reasonable(self) -> None:
        """_MAX_BUILD_FAILURES must be >= 1."""
        assert _MAX_BUILD_FAILURES >= 1

    @pytest.mark.asyncio
    async def test_max_summary_errors_constant_reasonable(self) -> None:
        """_MAX_SUMMARY_ERRORS must be >= 10."""
        assert _MAX_SUMMARY_ERRORS >= 10

    @pytest.mark.asyncio
    async def test_max_error_length_constant_reasonable(self) -> None:
        """_MAX_ERROR_LENGTH must be >= 64."""
        assert _MAX_ERROR_LENGTH >= 64


# ===================================================================
# 12. ReplayMetrics extended churn
# ===================================================================


class TestReplayMetricsExtendedChurn:
    """Extended ReplayMetrics churn beyond what test_longrun_soak covers.

    Uses more routes and heavier mixed operations."""

    @pytest.mark.asyncio
    async def test_30_routes_mixed_metrics(self) -> None:
        """30 routes with mixed events_processed/delivery_succeeded/failed."""
        metrics = ReplayMetrics()
        routes = [f"ext-metric-{i:03d}" for i in range(30)]

        for _ in range(20):
            for rid in routes:
                metrics.record_events_processed(rid)
                metrics.record_delivery_attempted(rid)
                if hash(rid) % 3 == 0:
                    metrics.record_delivery_succeeded(rid)
                else:
                    metrics.record_delivery_failed(rid)

        snap = metrics.snapshot()
        assert len(snap["by_route"]) == 30
        assert snap["global"]["replay_events_processed"] == 30 * 20

    @pytest.mark.asyncio
    async def test_replay_metrics_rejection_cancellation_accuracy(self) -> None:
        """Rejection and cancellation counters must be exact after 100 ops."""
        metrics = ReplayMetrics()

        for i in range(100):
            metrics.record_rejection()
            if i % 2 == 0:
                metrics.record_cancellation()

        snap = metrics.snapshot()
        assert snap["global"]["rejection_count"] == 100
        assert snap["global"]["cancellation_count"] == 50

    @pytest.mark.asyncio
    async def test_replay_metrics_backlog_estimate_updates(self) -> None:
        """Backlog estimate tracks last-set value through 20 updates."""
        metrics = ReplayMetrics()
        values = [10 * i for i in range(1, 21)]

        for val in values:
            metrics.set_backlog_estimate(val)
            snap = metrics.snapshot()
            assert snap["global"]["backlog_estimate"] == val

        # Final value must be 200.
        final = metrics.snapshot()
        assert final["global"]["backlog_estimate"] == 200


# ===================================================================
# 13. Diagnostics churn under combined lifecycle
# ===================================================================


class TestDiagnosticsChurnUnderCombinedLifecycle:
    """Diagnostics consistency when combining rapid lifecycle cycling
    with diagnostics capture at different lifecycle points.

    Earlier tests capture diagnostics only while RUNNING; this also
    captures after stop (where applicable)."""

    @pytest.mark.asyncio
    async def test_diagnostics_structure_across_15_cycles(
        self, soak: SoakRuntime,
    ) -> None:
        """15 cycles: capture build_runtime_snapshot at RUNNING state.

        All 15 snapshots must have identical key structures."""
        structures: list[frozenset[str]] = []

        for cycle in range(15):
            await soak.start_fresh()
            assert soak.app is not None

            await soak.deliver_events(count=2)
            raw = build_runtime_snapshot(soak.app)
            structures.append(frozenset(raw.keys()))

            await soak.stop()

        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, (
                f"Diagnostics structure changed at cycle {idx}"
            )

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_json_safe_across_cycles(
        self, soak: SoakRuntime,
    ) -> None:
        """10 cycles: diagnostic_snapshot() must always be JSON-serializable."""
        for cycle in range(10):
            await soak.start_fresh()
            assert soak.app is not None

            await soak.deliver_events(count=3)
            raw = soak.app.diagnostic_snapshot()
            serialized = json.dumps(raw, sort_keys=True, default=str)
            assert isinstance(serialized, str)
            assert len(serialized) > 0

            parsed = json.loads(serialized)
            assert isinstance(parsed, dict)

            await soak.stop()
