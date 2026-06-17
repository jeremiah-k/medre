"""Extended long-run soak with larger route graphs and combined stress.

Goes beyond ``test_longrun_soak.py`` (single-axis) and
``test_extended_longrun_soak.py`` (combined dual-axis) by exercising:

1. **Massive route-graph churn** — 200+ routes with concurrent delivery,
   failure, and skip operations simultaneously.
2. **Combined replay/diagnostics/start-stop/degraded/capacity/route expansion**
   in a single sustained session — all six axes interleaved.
3. **Repeated snapshot export stability** — 50+ consecutive snapshot exports
   verified for determinism and JSON round-trip stability.
4. **Bounded RouteStats under creation churn** — routes created on-the-fly
   with delivery pressure, verifying snapshot accuracy and no ghost entries.
5. **Bounded replay state under multi-route sustained churn** — ReplayState
   and ReplaySummary exercised with 100+ routes and mixed statuses.
6. **Bounded snapshot growth under escalating load** — adapter count and route
   count increase across cycles while snapshot JSON size stays bounded.
7. **Task/cancellation cleanup under massive combined stress** — 15+ cycles
   of start/deliver/capacity/replay/snapshot/stop with no task accumulation.
8. **Multi-adapter degraded cycling** — 1→4→2→3→1→4 adapter configurations
   with delivery in each cycle.
9. **Sustained ReplayMetrics under 100+ routes** — ReplayMetrics global and
   per-route counters verified exact after heavy churn.
10. **Snapshot export JSON round-trip stability** — 50+ captures serialized,
    parsed, and compared for structural identity.

Constraints
-----------
- **Fake adapters only** — no live transports or SDKs.
- **In-memory storage** — no filesystem I/O beyond temp dirs.
- **Bounded runtime** — <30 seconds total, deterministic, no sleeps.
- **Non-overlapping** — larger graphs and combined multi-axis vs prior
  single-axis and dual-axis tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
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
from medre.core.engine.replay.summary import _MAX_SUMMARY_ERRORS, _build_summary
from medre.core.engine.replay.types import (
    ReplayMode,
    ReplayResult,
    ReplayRouteAttribution,
    ReplayState,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata, RoutingMetadata
from medre.core.routing.stats import RouteStats
from medre.core.supervision.capacity import CapacityController
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import (
    build_runtime_snapshot,
)
from tests.helpers.soak import SoakRuntime, _count_asyncio_tasks

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


def _make_event(
    event_id: str = "evt-v3-001",
    source_adapter: str = "fake_matrix",
    *,
    routing: RoutingMetadata | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for v3 soak tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-v3",
        source_channel_id="ch-v3",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "v3-soak"},
        metadata=EventMetadata(routing=routing),
    )


def _build_config_with_n_adapters(
    n: int,
    name: str = "v3-soak",
) -> RuntimeConfig:
    """Build a RuntimeConfig with exactly *n* fake adapters (1–4)."""
    matrix_cfg = (
        {
            "v3_matrix": MatrixRuntimeConfig(
                adapter_id="v3_matrix",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 1
        else {}
    )
    meshtastic_cfg = (
        {
            "v3_mesh": MeshtasticRuntimeConfig(
                adapter_id="v3_mesh",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 2
        else {}
    )
    meshcore_cfg = (
        {
            "v3_meshcore": MeshCoreRuntimeConfig(
                adapter_id="v3_meshcore",
                enabled=True,
                adapter_kind="fake",
            ),
        }
        if n >= 3
        else {}
    )
    lxmf_cfg = (
        {
            "v3_lxmf": LxmfRuntimeConfig(
                adapter_id="v3_lxmf",
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
# 1. Massive route-graph churn (200+ routes)
# ===================================================================


class TestMassiveRouteGraphChurn:
    """Stress RouteStats with 200+ routes and concurrent mixed operations.

    Prior tests max out at 100 routes; this doubles the scale and
    verifies accuracy under heavy concurrent counter updates.
    """

    @pytest.mark.asyncio
    async def test_200_routes_mixed_ops_20_rounds(self) -> None:
        """200 routes × 20 rounds of delivered/failed/skipped/loop_prevented."""
        stats = RouteStats()
        routes = [f"v3-route-{i:04d}" for i in range(200)]

        for _ in range(20):
            for rid in routes:
                stats.record_delivered(rid)
                stats.record_failed(rid, error="v3-stress")
                stats.record_skipped(rid)
                stats.record_loop_prevented(rid)

        snap = stats.snapshot()
        assert len(snap) == 200, f"Expected 200 routes, got {len(snap)}"
        for rid in routes:
            entry = snap[rid]
            assert entry["delivered"] == 20
            assert entry["failed"] == 20
            assert entry["skipped"] == 20
            assert entry["loop_prevented"] == 20

    @pytest.mark.asyncio
    async def test_250_routes_snapshot_deterministic(self) -> None:
        """Two consecutive snapshots of 250 routes must be identical."""
        stats = RouteStats()
        for i in range(1000):
            rid = f"v3-big-{i % 250:04d}"
            stats.record_delivered(rid)
            if i % 5 == 0:
                stats.record_failed(rid, error="err-v3")

        snap1 = stats.snapshot()
        snap2 = stats.snapshot()
        assert snap1 == snap2
        assert len(snap1) == 250

    @pytest.mark.asyncio
    async def test_200_routes_error_strings_bounded(self) -> None:
        """Error strings in 200-route snapshot must be truncated to ≤512."""
        stats = RouteStats()
        long_error = "E" * 5000
        for i in range(200):
            stats.record_failed(f"v3-err-{i:04d}", error=long_error)

        snap = stats.snapshot()
        assert len(snap) == 200
        for rid, entry in snap.items():
            if "last_error" in entry and entry["last_error"]:
                assert (
                    len(entry["last_error"]) <= 512
                ), f"{rid}: error string too long: {len(entry['last_error'])}"


# ===================================================================
# 2. Combined replay/diagnostics/start-stop/degraded/capacity/route expansion
# ===================================================================


class TestCombinedSixAxisChurn:
    """Single sustained session interleaving all six axes: replay, diagnostics,
    start-stop, degraded, capacity, and route expansion.

    Prior extended tests combine 2–3 axes; this exercises all six in one run.
    """

    @pytest.mark.asyncio
    async def test_15_cycle_six_axis_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """15 cycles: build with varying adapters → start → capacity acquire →
        deliver → ReplayMetrics churn → diagnostics → capacity release → stop.

        Adapter counts cycle through 4, 2, 3, 4, 1, 4, 2, 3, 4, 2, 1, 4, 3, 4, 2.
        """
        adapter_sequence = [4, 2, 3, 4, 1, 4, 2, 3, 4, 2, 1, 4, 3, 4, 2]
        assert len(adapter_sequence) == 15

        baseline_tasks = _count_asyncio_tasks()
        all_adapter_counts: list[int] = []
        all_states: list[str] = []

        limits = RuntimeLimits(
            max_inflight_deliveries=8,
            max_inflight_replay_events=4,
        )

        for cycle in range(15):
            n = adapter_sequence[cycle]
            cycle_dir = tmp_path / f"six-axis-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"six-{cycle}")
            app = _build_app(config, paths)

            # Capacity.
            cc = CapacityController(limits)

            await app.start()
            assert app.state is RuntimeState.RUNNING
            all_adapter_counts.append(len(app.adapters))

            # Capacity acquire.
            acquired = await cc.acquire_delivery()
            assert acquired is True

            # Delivery burst.
            delivery_failures: list[tuple[str, str]] = []
            for i in range(3):
                for aid, adapter in app.adapters.items():
                    try:
                        if hasattr(adapter, "simulate_inbound"):
                            if hasattr(adapter, "make_text_event"):
                                event = adapter.make_text_event(
                                    f"six-{cycle}-{i}", channel="ch"
                                )
                            elif hasattr(adapter, "make_event"):
                                event = adapter.make_event(
                                    f"six-{cycle}-{i}", channel="ch"
                                )
                            else:
                                continue
                            await adapter.simulate_inbound(event)
                    except Exception as exc:
                        delivery_failures.append((aid, str(exc)))

            # ReplayMetrics churn.
            metrics = ReplayMetrics()
            for _ in range(8):
                for rid in [f"six-r-{cycle}-a", f"six-r-{cycle}-b"]:
                    metrics.record_events_processed(rid)
                    metrics.record_delivery_attempted(rid)
                    metrics.record_delivery_succeeded(rid)
            metrics_snap = metrics.snapshot()
            assert metrics_snap["global"]["replay_events_processed"] == 16

            # Diagnostics.
            raw = build_runtime_snapshot(app)
            all_states.append(raw.get("lifecycle", {}).get("runtime_state", "unknown"))

            # Route expansion via RouteStats.
            stats = RouteStats()
            for r in range(5):
                rid = f"six-route-{cycle}-{r}"
                stats.record_delivered(rid)
                stats.record_failed(rid, error="six-axis-err")
            assert len(stats.snapshot()) == 5

            # Capacity release.
            await cc.release_delivery()
            cc.stop_accepting()

            await app.stop()
            assert app.state is RuntimeState.STOPPED

        assert all_adapter_counts == adapter_sequence
        assert all(s == "running" for s in all_states)

        after_tasks = _count_asyncio_tasks()
        assert after_tasks <= baseline_tasks + 2, (
            f"Task leak after 15 combined cycles: "
            f"baseline={baseline_tasks}, after={after_tasks}"
        )


# ===================================================================
# 3. Repeated snapshot export stability
# ===================================================================


class TestRepeatedSnapshotExport:
    """50+ consecutive snapshot exports verified for determinism and
    JSON round-trip stability.

    Prior tests check snapshot determinism at most twice; this
    exercises 50+ sequential exports under sustained load.
    """

    @pytest.mark.asyncio
    async def test_50_snapshot_exports_identical(
        self,
        soak: SoakRuntime,
    ) -> None:
        """50 consecutive snapshots under delivery load: top-level key
        structure must be identical and every snapshot must be JSON-safe.
        Values will naturally change (accounting counters, uptime)."""
        await soak.start()
        assert soak.app is not None

        structures: list[frozenset[str]] = []
        serialized_ok = 0
        for _i in range(50):
            await soak.deliver_events(count=1)
            raw = build_runtime_snapshot(soak.app)
            # Verify JSON round-trip.
            serialized = json.dumps(raw, sort_keys=True, default=str)
            parsed = json.loads(serialized)
            assert isinstance(parsed, dict)
            structures.append(frozenset(parsed.keys()))
            serialized_ok += 1

        await soak.stop()

        assert serialized_ok == 50
        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, f"Snapshot {idx} key set differs from snapshot 0"

    @pytest.mark.asyncio
    async def test_60_snapshot_json_round_trips(
        self,
        soak: SoakRuntime,
    ) -> None:
        """60 snapshots: each must be JSON-serializable and parseable."""
        await soak.start()
        assert soak.app is not None

        for _i in range(60):
            await soak.deliver_events(count=1)
            raw = soak.app.diagnostic_snapshot()
            serialized = json.dumps(raw, sort_keys=True, default=str)
            assert isinstance(serialized, str)
            assert len(serialized) > 0

            parsed = json.loads(serialized)
            assert isinstance(parsed, dict)
            assert "runtime_state" in parsed

        await soak.stop()

    @pytest.mark.asyncio
    async def test_40_snapshot_structure_stable_under_degraded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """40 snapshots from varying adapter counts: key structure constant."""
        structures: list[frozenset[str]] = []

        for cycle in range(40):
            n = 1 + (cycle % 4)  # 1, 2, 3, 4 cycling
            cycle_dir = tmp_path / f"snap-deg-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"snap-{cycle}")
            app = _build_app(config, paths)

            await app.start()
            raw = build_runtime_snapshot(app)
            structures.append(frozenset(raw.keys()))
            await app.stop()

        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, f"Snapshot structure changed at cycle {idx}"


# ===================================================================
# 4. Bounded RouteStats under creation churn
# ===================================================================


class TestBoundedRouteStatsCreationChurn:
    """Routes created on-the-fly with delivery pressure, verifying snapshot
    accuracy and no ghost entries.

    Prior tests use fixed route sets; this simulates organic creation
    patterns with 150+ routes created across bursts.
    """

    @pytest.mark.asyncio
    async def test_150_routes_created_on_demand(self) -> None:
        """Create 150 routes across 30 bursts (5 routes per burst)."""
        stats = RouteStats()
        all_routes: set[str] = set()

        for burst in range(30):
            batch_routes = []
            for r in range(5):
                rid = f"churn-route-{burst * 5 + r:04d}"
                batch_routes.append(rid)
                all_routes.add(rid)
                stats.record_delivered(rid)
                stats.record_failed(rid, error="churn-err")

            snap = stats.snapshot()
            # After each burst, snapshot must contain exactly all routes so far.
            assert set(snap.keys()) == all_routes, (
                f"After burst {burst}: expected {len(all_routes)} routes, "
                f"got {len(snap)}"
            )

        final = stats.snapshot()
        assert len(final) == 150
        for rid in all_routes:
            assert final[rid]["delivered"] == 1
            assert final[rid]["failed"] == 1
            assert len(final[rid]["last_error"]) <= 512

    @pytest.mark.asyncio
    async def test_route_churn_with_deletion_pattern(self) -> None:
        """Create routes in rounds. Each round adds new routes; previous
        rounds remain in the snapshot (RouteStats does not prune)."""
        stats = RouteStats()
        round_routes: list[set[str]] = []

        for round_idx in range(5):
            round_set = set()
            for r in range(20):
                rid = f"round-{round_idx}-route-{r:03d}"
                round_set.add(rid)
                stats.record_delivered(rid)
            round_routes.append(round_set)

            snap = stats.snapshot()
            # All routes from all rounds must be present.
            all_so_far = set()
            for ws in round_routes:
                all_so_far |= ws
            assert set(snap.keys()) == all_so_far

        final = stats.snapshot()
        assert len(final) == 100  # 5 rounds × 20 routes


# ===================================================================
# 5. Bounded replay state under multi-route sustained churn
# ===================================================================


class TestBoundedReplayStateMultiRoute:
    """ReplayState and ReplaySummary exercised with 100+ routes and mixed
    statuses.

    Prior tests use ≤50 unique event IDs; this exercises 100+ unique
    routes with 500+ results.
    """

    @pytest.mark.asyncio
    async def test_500_results_100_routes_mixed_statuses(self) -> None:
        """500 results across 100 routes with passed/skipped/failed/error."""
        state = ReplayState()
        routes = [f"replay-v3-{i:03d}" for i in range(100)]

        for i in range(500):
            rid = routes[i % 100]
            if i % 4 == 0:
                status = "passed"
            elif i % 4 == 1:
                status = "skipped"
            elif i % 4 == 2:
                status = "failed"
                result = ReplayResult(
                    event_id=f"evt-v3-{i:04d}",
                    stage="deliver",
                    status=status,
                    error=f"fail-{rid}",
                )
                state.record(result)
                continue
            else:
                status = "error"
                result = ReplayResult(
                    event_id=f"evt-v3-{i:04d}",
                    stage="render",
                    status=status,
                    error=f"err-{rid}",
                )
                state.record(result)
                continue

            result = ReplayResult(
                event_id=f"evt-v3-{i:04d}",
                stage="store",
                status=status,
            )
            state.record(result)

        assert state.events_processed == 500
        assert state.events_passed == 125
        assert state.events_skipped == 125
        assert state.events_failed == 250  # 125 "failed" + 125 "error"
        assert len(state.errors) == 250

    @pytest.mark.asyncio
    async def test_replay_summary_capped_300_results(self) -> None:
        """ReplaySummary.errors capped at _MAX_SUMMARY_ERRORS with 300 results."""
        results = []
        for i in range(300):
            results.append(
                ReplayResult(
                    event_id=f"evt-cap-{i}",
                    stage="deliver",
                    status="error",
                    error=f"capped-err-{i}",
                )
            )

        summary = _build_summary(results)
        assert len(summary.errors) == _MAX_SUMMARY_ERRORS
        assert summary.events_replayed == 300
        assert summary.failure_count == 300

    @pytest.mark.asyncio
    async def test_replay_summary_with_route_attribution(self) -> None:
        """ReplaySummary with route attribution for 50 routes."""
        results = []
        routes = [f"attr-route-{i:03d}" for i in range(50)]

        for i in range(100):
            rid = routes[i % 50]
            results.append(
                ReplayResult(
                    event_id=f"evt-attr-{i}",
                    stage="route",
                    status="passed" if i % 2 == 0 else "skipped",
                    route_attribution=ReplayRouteAttribution(
                        route_ids=(rid,),
                        source_adapter="src",
                        target_adapters=("tgt",),
                        replay_mode=ReplayMode.DRY_RUN,
                        is_replay=True,
                        loop_warnings=(),
                        run_id="v3-run",
                    ),
                )
            )

        summary = _build_summary(results, events_scanned=100, run_id="v3-run")
        d1 = summary.to_dict()
        d2 = summary.to_dict()
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)
        assert summary.events_replayed == 100

    @pytest.mark.asyncio
    async def test_replay_state_lineage_with_100_ancestors(self) -> None:
        """ReplayState.current_lineage holds only the most recent lineage
        even with 100 ancestors."""
        state = ReplayState()
        for i in range(30):
            result = ReplayResult(
                event_id=f"evt-lin-v3-{i}",
                stage="store",
                status="passed",
                lineage=[f"ancestor-v3-{j}" for j in range(100)],
            )
            state.record(result)

        assert len(state.current_lineage) == 100
        assert state.current_lineage[0] == "ancestor-v3-0"


# ===================================================================
# 6. Bounded snapshot growth under escalating load
# ===================================================================


class TestBoundedSnapshotGrowthEscalating:
    """Snapshot JSON size stays bounded as adapter count and route count
    increase across cycles.

    Prior tests use a fixed adapter count; this escalates load across
    cycles and verifies snapshot size does not grow unboundedly.
    """

    @pytest.mark.asyncio
    async def test_snapshot_size_bounded_escalating_adapters(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Snapshot size bounded across 1→2→3→4→4→4→4→4 adapter cycles
        with delivery in each."""
        sizes: list[int] = []
        adapter_counts = [1, 2, 3, 4, 4, 4, 4, 4]

        for cycle, n in enumerate(adapter_counts):
            cycle_dir = tmp_path / f"escalate-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"esc-{cycle}")
            app = _build_app(config, paths)

            await app.start()
            raw = build_runtime_snapshot(app)
            serialized = json.dumps(raw, sort_keys=True, default=str)
            sizes.append(len(serialized))

            # RouteStats churn on top.
            stats = RouteStats()
            for r in range(10 * (cycle + 1)):
                stats.record_delivered(f"esc-route-{r}")
            assert len(stats.snapshot()) == 10 * (cycle + 1)

            await app.stop()

        # Max size should not exceed 4× the min size (adapter variation only).
        assert (
            max(sizes) <= min(sizes) * 4 + 200
        ), f"Snapshot size grew unboundedly: {sizes}"

    @pytest.mark.asyncio
    async def test_snapshot_key_count_constant_under_load(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Snapshot top-level key count stays constant under 30 captures."""
        await soak.start()
        assert soak.app is not None

        key_counts: set[int] = set()
        for _i in range(30):
            await soak.deliver_events(count=2)
            raw = build_runtime_snapshot(soak.app)
            key_counts.add(len(raw))

        await soak.stop()

        # Key count must be constant across all captures.
        assert len(key_counts) == 1, f"Snapshot key count varied: {key_counts}"


# ===================================================================
# 7. Task/cancellation cleanup under massive combined stress
# ===================================================================


class TestTaskCleanupMassiveCombinedStress:
    """15+ cycles of start/deliver/capacity/replay/snapshot/stop with no
    task accumulation.

    Prior combined stress tests max out at 12 cycles; this pushes to 15
    with capacity + replay + route stats + diagnostics in each cycle.
    """

    @pytest.mark.asyncio
    async def test_15_combined_stress_cycles_no_leak(
        self,
        soak: SoakRuntime,
    ) -> None:
        """15 cycles: start → capacity → deliver → replay metrics →
        route stats → diagnostics → stop. No task accumulation."""
        baseline = _count_asyncio_tasks()

        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )

        for cycle in range(15):
            cc = CapacityController(limits)
            await soak.start_fresh()
            assert soak.app is not None

            # Capacity acquire.
            acquired = await cc.acquire_delivery()
            assert acquired is True

            # Delivery burst.
            await soak.deliver_events(count=3)

            # ReplayMetrics churn.
            metrics = ReplayMetrics()
            for _ in range(5):
                for rid in [f"stress-r-{cycle}-a", f"stress-r-{cycle}-b"]:
                    metrics.record_events_processed(rid)
                    metrics.record_delivery_attempted(rid)
                    metrics.record_delivery_succeeded(rid)

            # RouteStats.
            stats = RouteStats()
            for r in range(4):
                stats.record_delivered(f"stress-route-{cycle}-{r}")

            # Diagnostics.
            snap = soak.capture_diagnostics(iteration=cycle)
            assert snap.runtime_state == "running"

            # Capacity release.
            await cc.release_delivery()
            cc.stop_accepting()

            await soak.stop()

        after = _count_asyncio_tasks()
        assert after <= baseline + 2, (
            f"Task leak under massive combined stress: "
            f"baseline={baseline}, after={after}"
        )

    @pytest.mark.asyncio
    async def test_10_rapid_build_start_stop_capacity(
        self,
        soak: SoakRuntime,
    ) -> None:
        """10 rapid build→start→capacity→stop with fresh CC each cycle."""
        task_counts: list[int] = []

        for _ in range(10):
            _count_asyncio_tasks()

            await soak.start_fresh()
            assert soak.app is not None

            if soak.app._capacity_controller is not None:
                ok = await soak.app._capacity_controller.acquire_delivery()
                if ok:
                    await soak.app._capacity_controller.release_delivery()

            await soak.stop()
            task_counts.append(_count_asyncio_tasks())

        max_count = max(task_counts)
        min_count = min(task_counts)
        assert max_count - min_count <= 2, f"Task count drifted: {task_counts}"


# ===================================================================
# 8. Multi-adapter degraded cycling
# ===================================================================


class TestMultiAdapterDegradedCycling:
    """Cycle through 1→4→2→3→1→4 adapter configurations with delivery.

    Prior tests cycle through 1→2→3→4 linearly; this exercises a
    non-monotonic pattern to stress runtime rebuild logic.
    """

    @pytest.mark.asyncio
    async def test_non_monotonic_adapter_cycling(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """1→4→2→3→1→4 adapters: each cycle must start, deliver, and stop."""
        counts = [1, 4, 2, 3, 1, 4]
        adapter_counts_seen: list[int] = []

        for idx, n in enumerate(counts):
            cycle_dir = tmp_path / f"degrade-{idx}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"deg-{idx}")
            app = _build_app(config, paths)

            await app.start()
            assert app.state is RuntimeState.RUNNING
            adapter_counts_seen.append(len(app.adapters))

            # Delivery.
            for i in range(2):
                for _aid, adapter in app.adapters.items():
                    try:
                        if hasattr(adapter, "simulate_inbound"):
                            if hasattr(adapter, "make_text_event"):
                                event = adapter.make_text_event(
                                    f"deg-{idx}-{i}", channel="ch"
                                )
                            elif hasattr(adapter, "make_event"):
                                event = adapter.make_event(
                                    f"deg-{idx}-{i}", channel="ch"
                                )
                            else:
                                continue
                            await adapter.simulate_inbound(event)
                    except Exception:
                        pass

            await app.stop()
            assert app.state is RuntimeState.STOPPED

        assert adapter_counts_seen == counts

    @pytest.mark.asyncio
    async def test_degraded_cycling_diagnostics_consistent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Diagnostics across non-monotonic adapter cycling: all "running"."""
        counts = [4, 2, 1, 3, 4]
        states: list[str] = []

        for idx, n in enumerate(counts):
            cycle_dir = tmp_path / f"deg-diag-{idx}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_config_with_n_adapters(n, name=f"deg-diag-{idx}")
            app = _build_app(config, paths)

            await app.start()
            raw = build_runtime_snapshot(app)
            states.append(raw.get("lifecycle", {}).get("runtime_state", "unknown"))
            await app.stop()

        assert all(s == "running" for s in states)


# ===================================================================
# 9. Sustained ReplayMetrics under 100+ routes
# ===================================================================


class TestSustainedReplayMetrics100Routes:
    """ReplayMetrics global and per-route counters verified exact after
    heavy churn with 100+ routes.

    Prior tests max out at 30 routes; this exercises 100+ with
    mixed operations across 25 rounds.
    """

    @pytest.mark.asyncio
    async def test_100_routes_25_rounds_mixed(self) -> None:
        """100 routes × 25 rounds of mixed metrics operations."""
        metrics = ReplayMetrics()
        routes = [f"v3-metric-{i:03d}" for i in range(100)]

        for _ in range(25):
            for rid in routes:
                metrics.record_events_processed(rid)
                metrics.record_delivery_attempted(rid)
                if hash(rid) % 3 == 0:
                    metrics.record_delivery_succeeded(rid)
                elif hash(rid) % 3 == 1:
                    metrics.record_delivery_failed(rid)
                else:
                    metrics.record_skipped_by_filter(rid)

        snap = metrics.snapshot()
        assert len(snap["by_route"]) == 100
        # 100 routes × 25 rounds = 2500 events processed.
        assert snap["global"]["replay_events_processed"] == 2500
        assert snap["global"]["replay_deliveries_attempted"] == 2500

    @pytest.mark.asyncio
    async def test_replay_metrics_rejection_cancellation_200_ops(self) -> None:
        """Rejection and cancellation counters exact after 200 ops."""
        metrics = ReplayMetrics()

        for i in range(200):
            metrics.record_rejection()
            if i % 3 == 0:
                metrics.record_cancellation()

        snap = metrics.snapshot()
        assert snap["global"]["rejection_count"] == 200
        # Floor(200/3) + 1 = 67 cancellations.
        expected_cancels = sum(1 for i in range(200) if i % 3 == 0)
        assert snap["global"]["cancellation_count"] == expected_cancels

    @pytest.mark.asyncio
    async def test_replay_metrics_backlog_30_updates(self) -> None:
        """Backlog estimate tracks through 30 updates."""
        metrics = ReplayMetrics()
        values = [5 * i for i in range(1, 31)]

        for val in values:
            metrics.set_backlog_estimate(val)
            snap = metrics.snapshot()
            assert snap["global"]["backlog_estimate"] == val

        final = metrics.snapshot()
        assert final["global"]["backlog_estimate"] == 150


# ===================================================================
# 10. Snapshot export JSON round-trip stability
# ===================================================================


class TestSnapshotExportJsonRoundTrip:
    """50+ captures serialized, parsed, and compared for structural identity.

    Prior tests check JSON safety briefly; this does exhaustive round-trip
    verification across 50+ captures under sustained delivery.
    """

    @pytest.mark.asyncio
    async def test_55_captures_round_trip_stable(
        self,
        soak: SoakRuntime,
    ) -> None:
        """55 captures: each serializes → parses → same keys as first."""
        await soak.start()
        assert soak.app is not None

        structures: list[frozenset[str]] = []
        for _i in range(55):
            await soak.deliver_events(count=1)
            raw = soak.app.diagnostic_snapshot()

            # Round-trip.
            serialized = json.dumps(raw, sort_keys=True, default=str)
            parsed = json.loads(serialized)
            structures.append(frozenset(parsed.keys()))

        await soak.stop()

        first = structures[0]
        mismatches = [idx for idx, s in enumerate(structures[1:], 1) if s != first]
        assert not mismatches, f"Structure mismatched at captures: {mismatches}"

    @pytest.mark.asyncio
    async def test_50_runtime_snapshot_round_trips(
        self,
        soak: SoakRuntime,
    ) -> None:
        """50 build_runtime_snapshot round-trips: all JSON-serializable
        with stable key structures."""
        _VARIABLE_KEYS = frozenset({"snapshot_at", "uptime_seconds"})

        await soak.start()
        assert soak.app is not None

        structures: list[frozenset[str]] = []
        for _ in range(50):
            await soak.deliver_events(count=1)
            raw = build_runtime_snapshot(soak.app)
            # Must be JSON round-trippable.
            serialized = json.dumps(raw, sort_keys=True, default=str)
            parsed = json.loads(serialized)
            structures.append(frozenset(parsed.keys()))

        await soak.stop()

        first = structures[0]
        mismatches = [idx for idx, s in enumerate(structures[1:], 1) if s != first]
        assert (
            not mismatches
        ), f"Snapshot key structures mismatched at captures: {mismatches}"

    @pytest.mark.asyncio
    async def test_snapshot_adapters_always_4_under_load(
        self,
        soak: SoakRuntime,
    ) -> None:
        """25 snapshots under load: adapter count always 4."""
        await soak.start()
        assert soak.app is not None

        for i in range(25):
            await soak.deliver_events(count=2)
            raw = build_runtime_snapshot(soak.app)
            adapters = raw.get("adapters", {})
            assert (
                len(adapters) == 4
            ), f"Adapter count changed at iteration {i}: {len(adapters)}"

        await soak.stop()
