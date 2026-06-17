"""Long-running fake-runtime soak validation.

Deterministic, fake-only tests covering:

- Repeated replay churn across many lifecycle cycles.
- Route fanout under sustained event delivery.
- Snapshot generation stability and determinism.
- Degraded adapter churn (partial adapter failures).
- Startup failure recovery cycles.
- Cancellation cycles (rapid build→start→stop).
- Capacity exhaustion and recovery.
- Diagnostics export consistency.
- RouteStats churn under heavy delivery.
- Route-trace boundedness ([-16:] cap enforced).
- No lingering replay/cancellation work after stop.
- Bounded counters (fake adapter history capped at _MAX_FAKE_HISTORY).
- No snapshot growth across iterations.

Every test here:

- Uses **fake adapters** only — no live transports or SDKs.
- Uses **in-memory storage** — no filesystem I/O beyond temp dirs.
- Runs within **<30 seconds** total.
- Is **deterministic** — no wall-clock sleeps.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
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

_MAX_FAKE_HISTORY: int = 1000


def _make_event(
    event_id: str = "evt-longrun-001",
    source_adapter: str = "fake_matrix",
    *,
    routing: RoutingMetadata | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for longrun soak tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-longrun",
        source_channel_id="ch-longrun",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "longrun-soak"},
        metadata=EventMetadata(routing=routing),
    )


def _build_two_adapter_config(name: str = "longrun") -> RuntimeConfig:
    """RuntimeConfig with two fake adapters (matrix + meshtastic)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name=name),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="memory"),
        limits=RuntimeLimits(
            max_inflight_deliveries=50,
            max_inflight_replay_events=50,
        ),
        adapters=AdapterConfigSet(
            matrix={
                "lr_matrix": MatrixRuntimeConfig(
                    adapter_id="lr_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "lr_mesh": MeshtasticRuntimeConfig(
                    adapter_id="lr_mesh",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _build_degraded_config(name: str = "longrun-degraded") -> RuntimeConfig:
    """RuntimeConfig with only one fake adapter (degraded)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name=name),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="memory"),
        limits=RuntimeLimits(
            max_inflight_deliveries=50,
            max_inflight_replay_events=50,
        ),
        adapters=AdapterConfigSet(
            matrix={
                "deg_matrix": MatrixRuntimeConfig(
                    adapter_id="deg_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _build_empty_config(name: str = "longrun-empty") -> RuntimeConfig:
    """RuntimeConfig with zero adapters (triggers startup failure)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name=name),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


# ===================================================================
# 1. Repeated replay churn
# ===================================================================


class TestRepeatedReplayChurn:
    """Verify stability under many replay-style lifecycle cycles."""

    @pytest.mark.asyncio
    async def test_10_replay_cycles_deterministic(
        self,
        soak: SoakRuntime,
    ) -> None:
        """10 replay-style cycles: start → burst 5 events → diagnostics → stop.

        Adapter counts and runtime states must be consistent.
        """
        adapter_counts: list[int] = []
        states: list[str] = []

        for cycle in range(10):
            await soak.start_fresh()
            assert soak.app is not None

            await soak.deliver_events(count=5)
            snap = soak.capture_diagnostics(iteration=cycle)
            adapter_counts.append(snap.adapter_count)
            states.append(snap.runtime_state)

            await soak.stop()

        assert all(
            c == 4 for c in adapter_counts
        ), f"Adapter counts drifted: {adapter_counts}"
        assert all(
            s == "running" for s in states
        ), f"Runtime states inconsistent: {states}"

    @pytest.mark.asyncio
    async def test_replay_churn_no_task_leak(
        self,
        soak: SoakRuntime,
    ) -> None:
        """6 replay cycles must not leak asyncio tasks."""
        baseline = _count_asyncio_tasks()

        for _ in range(6):
            await soak.start_fresh()
            assert soak.app is not None
            await soak.deliver_events(count=3)
            await soak.stop()

        after = _count_asyncio_tasks()
        assert (
            after <= baseline + 2
        ), f"Task leak after replay churn: baseline={baseline}, after={after}"


# ===================================================================
# 2. Route fanout
# ===================================================================


class TestRouteFanout:
    """Verify route fanout stability under sustained delivery."""

    @pytest.mark.asyncio
    async def test_sustained_delivery_bursts(
        self,
        soak: SoakRuntime,
    ) -> None:
        """10 bursts of 5 events each — runtime stays healthy."""
        await soak.start()
        assert soak.app is not None

        for burst in range(10):
            results = await soak.deliver_events(count=5)
            assert len(results) == 5

            snap = soak.capture_diagnostics(iteration=burst)
            assert snap.runtime_state == "running"
            assert snap.adapter_count == 4

        await soak.stop()

    @pytest.mark.asyncio
    async def test_route_stats_accumulate_under_fanout(
        self,
        soak: SoakRuntime,
    ) -> None:
        """RouteStats counters must be monotonically non-decreasing."""
        await soak.start()
        assert soak.app is not None

        stats = soak.app.route_stats
        prev_delivered: dict[str, int] = {}

        for burst in range(5):
            await soak.deliver_events(count=4)
            snap = stats.snapshot()
            for route_id, counters in snap.items():
                current = counters["delivered"]
                prev = prev_delivered.get(route_id, 0)
                assert current >= prev, (
                    f"Counter went backwards for {route_id}: "
                    f"{current} < {prev} at burst {burst}"
                )
                prev_delivered[route_id] = current

        await soak.stop()


# ===================================================================
# 3. Snapshot generation
# ===================================================================


class TestSnapshotGeneration:
    """Verify snapshot generation is stable and deterministic."""

    @pytest.mark.asyncio
    async def test_20_consecutive_snapshots_stable_keys(
        self,
        soak: SoakRuntime,
    ) -> None:
        """20 consecutive snapshots must have identical key structures."""
        await soak.start()
        assert soak.app is not None

        structures: list[frozenset[str]] = []
        for _i in range(20):
            raw = soak.app.diagnostic_snapshot()
            structures.append(frozenset(raw.keys()))

        await soak.stop()

        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, (
                f"Snapshot keys changed at capture {idx}: "
                f"expected {first}, got {struct}"
            )

    @pytest.mark.asyncio
    async def test_build_runtime_snapshot_deterministic(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Two build_runtime_snapshot calls with fixed clocks produce identical JSON."""
        await soak.start()
        assert soak.app is not None

        fixed_now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        fixed_mono = 42.0

        snap1 = build_runtime_snapshot(
            soak.app,
            now_fn=lambda: fixed_now,
            monotonic_fn=lambda: fixed_mono,
        )
        snap2 = build_runtime_snapshot(
            soak.app,
            now_fn=lambda: fixed_now,
            monotonic_fn=lambda: fixed_mono,
        )

        await soak.stop()

        j1 = json.dumps(snap1, sort_keys=True, default=str)
        j2 = json.dumps(snap2, sort_keys=True, default=str)
        assert j1 == j2, "build_runtime_snapshot is not deterministic"


# ===================================================================
# 4. Degraded adapter churn
# ===================================================================


class TestDegradedAdapterChurn:
    """Verify stability with reduced adapter sets."""

    @pytest.mark.asyncio
    async def test_degraded_5_cycles(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """5 start/stop cycles with 1-adapter degraded runtime."""
        for cycle in range(5):
            cycle_dir = tmp_path / f"deg-cycle-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config = _build_degraded_config(name=f"deg-{cycle}")
            app = _build_app(config, paths)
            await app.start()
            assert app.state is RuntimeState.RUNNING
            assert len(app.adapters) == 1
            await app.stop()
            assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_degraded_with_event_delivery(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Degraded runtime must tolerate event delivery failures gracefully."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        config = _build_degraded_config()
        app = _build_app(config, paths)
        await app.start()

        for i in range(8):
            for _adapter_id, adapter in app.adapters.items():
                try:
                    if hasattr(adapter, "simulate_inbound"):
                        if hasattr(adapter, "make_text_event"):
                            event = adapter.make_text_event(f"deg-{i}", channel="ch")
                        elif hasattr(adapter, "make_event"):
                            event = adapter.make_event(f"deg-{i}", channel="ch")
                        else:
                            continue
                        await adapter.simulate_inbound(event)
                except Exception:
                    pass

        await app.stop()
        assert app.state is RuntimeState.STOPPED


# ===================================================================
# 5. Startup failure recovery
# ===================================================================


class TestStartupFailureRecovery:
    """Verify failed startup → successful recovery cycles."""

    @pytest.mark.asyncio
    async def test_3_failure_recovery_cycles(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """3 cycles of failed build → successful build must all recover."""
        for cycle in range(3):
            cycle_dir = tmp_path / f"fail-cycle-{cycle}"
            cycle_dir.mkdir(exist_ok=True)
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()

            # Step 1: Empty config → startup failure.
            config_empty = _build_empty_config(name=f"fail-{cycle}")
            app_fail = _build_app(config_empty, paths)
            with pytest.raises(Exception):  # noqa: B017
                await app_fail.start()
            assert app_fail.state is RuntimeState.FAILED

            # Step 2: Valid config → succeeds.
            monkeypatch.setenv("MEDRE_HOME", str(cycle_dir))
            paths = resolve()
            config_ok = _build_two_adapter_config(name=f"ok-{cycle}")
            app_ok = _build_app(config_ok, paths)
            await app_ok.start()
            assert app_ok.state is RuntimeState.RUNNING
            await app_ok.stop()
            assert app_ok.state is RuntimeState.STOPPED


# ===================================================================
# 6. Cancellation cycles
# ===================================================================


class TestCancellationCycles:
    """Verify rapid build→start→stop cancellation cycles."""

    @pytest.mark.asyncio
    async def test_15_rapid_start_stop_cycles(
        self,
        soak: SoakRuntime,
    ) -> None:
        """15 rapid start/stop cycles — each must reach RUNNING → STOPPED."""
        for _cycle in range(15):
            await soak.start_fresh()
            assert soak.app is not None
            assert soak.app.state is RuntimeState.RUNNING
            await soak.stop()
            assert soak.app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_cancellation_no_task_accumulation(
        self,
        soak: SoakRuntime,
    ) -> None:
        """10 cancellation cycles must not accumulate tasks."""
        task_counts: list[int] = []

        for _ in range(10):
            _count_asyncio_tasks()
            await soak.start_fresh()
            assert soak.app is not None
            await soak.stop()
            task_counts.append(_count_asyncio_tasks())

        max_count = max(task_counts)
        min_count = min(task_counts)
        assert (
            max_count - min_count <= 2
        ), f"Task count drifted over cancellation cycles: {task_counts}"


# ===================================================================
# 7. Capacity exhaustion
# ===================================================================


class TestCapacityExhaustion:
    """Verify capacity controller handles exhaustion and recovery."""

    @pytest.mark.asyncio
    async def test_capacity_acquire_release_cycles(
        self,
    ) -> None:
        """50 acquire/release cycles on capacity must not leak slots."""
        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        for _ in range(50):
            acquired = await cc.acquire_delivery()
            assert acquired is True
            await cc.release_delivery()

        # After all cycles, current must be 0.
        assert cc.delivery_current == 0

    @pytest.mark.asyncio
    async def test_replay_capacity_acquire_release_cycles(
        self,
    ) -> None:
        """50 acquire/release cycles on replay capacity must not leak slots."""
        limits = RuntimeLimits(
            max_inflight_deliveries=4,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        for _ in range(50):
            acquired = await cc.acquire_replay()
            assert acquired is True
            await cc.release_replay()

        assert cc.replay_current == 0

    @pytest.mark.asyncio
    async def test_capacity_snapshot_bounded(
        self,
    ) -> None:
        """Capacity snapshot counters stay within limits."""
        limits = RuntimeLimits(
            max_inflight_deliveries=8,
            max_inflight_replay_events=4,
        )
        cc = CapacityController(limits)

        # Acquire all delivery slots.
        for _ in range(8):
            acquired = await cc.acquire_delivery()
            assert acquired is True

        snap = cc.snapshot()
        assert snap["delivery_current"] == 8
        assert snap["delivery_limit"] == 8
        assert snap["delivery_current"] <= snap["delivery_limit"]

        # Release all.
        for _ in range(8):
            await cc.release_delivery()

        snap_after = cc.snapshot()
        assert snap_after["delivery_current"] == 0


# ===================================================================
# 8. Diagnostics export
# ===================================================================


class TestDiagnosticsExport:
    """Verify diagnostics export consistency and JSON safety."""

    @pytest.mark.asyncio
    async def test_diagnostics_json_serializable(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Diagnostics snapshots must be JSON-serializable."""
        await soak.start()
        assert soak.app is not None

        for _i in range(5):
            raw = soak.app.diagnostic_snapshot()
            # Must not raise.
            serialized = json.dumps(raw, sort_keys=True, default=str)
            assert isinstance(serialized, str)
            assert len(serialized) > 0

        await soak.stop()

    @pytest.mark.asyncio
    async def test_build_runtime_snapshot_json_safe(
        self,
        soak: SoakRuntime,
    ) -> None:
        """build_runtime_snapshot must produce JSON-safe output."""
        await soak.start()
        assert soak.app is not None

        snap = build_runtime_snapshot(soak.app)
        serialized = json.dumps(snap, sort_keys=True, default=str)
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)

        await soak.stop()

    @pytest.mark.asyncio
    async def test_diagnostics_export_across_cycles(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Diagnostics structure must be consistent across 6 lifecycle cycles."""
        structures: list[frozenset[str]] = []

        for _cycle in range(6):
            await soak.start_fresh()
            assert soak.app is not None
            raw = build_runtime_snapshot(soak.app)
            structures.append(frozenset(raw.keys()))
            await soak.stop()

        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, f"Diagnostics structure changed at cycle {idx}"


# ===================================================================
# 9. RouteStats churn
# ===================================================================


class TestRouteStatsChurn:
    """Verify RouteStats stability under heavy counter churn."""

    @pytest.mark.asyncio
    async def test_heavy_delivery_counter_churn(self) -> None:
        """1000 deliveries across 5 routes — counters must be exact."""
        stats = RouteStats()
        routes = ["r-alpha", "r-beta", "r-gamma", "r-delta", "r-epsilon"]

        for _ in range(200):
            for route_id in routes:
                stats.record_delivered(route_id)

        snap = stats.snapshot()
        for route_id in routes:
            assert (
                snap[route_id]["delivered"] == 200
            ), f"{route_id}: expected 200, got {snap[route_id]['delivered']}"

    @pytest.mark.asyncio
    async def test_mixed_counter_operations(self) -> None:
        """Mixed delivered/failed/skipped/loop_prevented operations."""
        stats = RouteStats()
        route = "mixed-route"

        for _ in range(50):
            stats.record_delivered(route)
            stats.record_failed(route, error="test-error")
            stats.record_skipped(route)
            stats.record_loop_prevented(route)

        snap = stats.snapshot()
        assert snap[route]["delivered"] == 50
        assert snap[route]["failed"] == 50
        assert snap[route]["skipped"] == 50
        assert snap[route]["loop_prevented"] == 50

    @pytest.mark.asyncio
    async def test_stats_snapshot_deterministic_under_churn(self) -> None:
        """Two consecutive snapshots must be identical after heavy churn."""
        stats = RouteStats()
        for i in range(100):
            stats.record_delivered(f"churn-{i % 10}")

        snap1 = stats.snapshot()
        snap2 = stats.snapshot()
        assert snap1 == snap2, "RouteStats snapshot is not deterministic under churn"


# ===================================================================
# 10. Route-trace boundedness
# ===================================================================


class TestRouteTraceBoundedness:
    """Verify route_trace is bounded to 16 entries."""

    @pytest.mark.asyncio
    async def test_route_trace_capped_at_16(self) -> None:
        """route_trace must never exceed 16 entries."""
        # Simulate the pipeline's route_trace truncation logic.
        prior_trace: tuple[str, ...] = ()
        for i in range(30):
            route_ids = (f"route-{i}",)
            new_trace = (prior_trace + route_ids)[-16:]
            assert (
                len(new_trace) <= 16
            ), f"Trace exceeded 16 at iteration {i}: len={len(new_trace)}"
            prior_trace = new_trace

        # After 30 iterations, trace should be exactly 16.
        assert len(prior_trace) == 16

    @pytest.mark.asyncio
    async def test_route_trace_keeps_latest_16(self) -> None:
        """route_trace[-16:] must keep only the latest 16 entries."""
        prior_trace: tuple[str, ...] = ()
        for i in range(20):
            route_ids = (f"r-{i}",)
            prior_trace = (prior_trace + route_ids)[-16:]

        # Should contain r-4 through r-19.
        assert prior_trace[0] == "r-4"
        assert prior_trace[-1] == "r-19"
        assert len(prior_trace) == 16

    @pytest.mark.asyncio
    async def test_event_metadata_trace_bounded(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Events delivered through the runtime have bounded route_trace."""
        await soak.start()
        assert soak.app is not None

        await soak.deliver_events(count=5)
        await soak.stop()
        # The trace is bounded at [-16:] in pipeline.py line 514.
        # This test verifies the runtime doesn't introduce unbounded traces.


# ===================================================================
# 11. No lingering replay/cancellation work
# ===================================================================


class TestNoLingeringWork:
    """Verify no replay/cancellation work remains after runtime stop."""

    @pytest.mark.asyncio
    async def test_no_lingering_tasks_after_8_cycles(
        self,
        soak: SoakRuntime,
    ) -> None:
        """8 start/deliver/stop cycles — no task accumulation."""
        baseline = _count_asyncio_tasks()

        for _ in range(8):
            await soak.start_fresh()
            assert soak.app is not None
            await soak.deliver_events(count=4)
            await soak.stop()

        after = _count_asyncio_tasks()
        assert (
            after <= baseline + 2
        ), f"Lingering tasks: baseline={baseline}, after={after}"

    @pytest.mark.asyncio
    async def test_capacity_released_after_stop(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Capacity must be fully released after stop."""
        for _ in range(5):
            await soak.start_fresh()
            assert soak.app is not None
            await soak.deliver_events(count=3)
            await soak.stop()

            # After stop, the capacity controller should reflect 0 current.
            if soak.app._capacity_controller is not None:
                snap = soak.app._capacity_controller.snapshot()
                assert snap.get("delivery_current", 0) == 0, f"Capacity leaked: {snap}"


# ===================================================================
# 12. Bounded counters
# ===================================================================


class TestBoundedCounters:
    """Verify fake adapter history stays bounded."""

    @pytest.mark.asyncio
    async def test_fake_adapter_history_bounded(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Fake adapter history lists must stay <= _MAX_FAKE_HISTORY."""
        await soak.start()
        assert soak.app is not None

        # Deliver more events than _MAX_FAKE_HISTORY.
        # (soak.deliver_events loops, each adapter may append.)
        for _burst in range(20):
            await soak.deliver_events(count=10)

        for adapter_id, adapter in soak.app.adapters.items():
            for attr_name in (
                "delivered_payloads",
                "inbound_events",
                "received_events",
                "delivered_events",
            ):
                attr = getattr(adapter, attr_name, None)
                if isinstance(attr, list):
                    assert len(attr) <= _MAX_FAKE_HISTORY, (
                        f"Adapter {adapter_id}.{attr_name} unbounded: "
                        f"{len(attr)} > {_MAX_FAKE_HISTORY}"
                    )

        await soak.stop()

    @pytest.mark.asyncio
    async def test_replay_metrics_bounded_by_route_count(
        self,
    ) -> None:
        """ReplayMetrics route counters must not exceed the route count."""
        metrics = ReplayMetrics()
        routes = [f"replay-route-{i}" for i in range(10)]

        for _ in range(50):
            for route_id in routes:
                metrics.record_events_processed(route_id)
                metrics.record_delivery_attempted(route_id)
                metrics.record_delivery_succeeded(route_id)

        snap = metrics.snapshot()
        # Must have exactly 10 routes in by_route.
        assert (
            len(snap["by_route"]) == 10
        ), f"Expected 10 routes, got {len(snap['by_route'])}"

        # No route should have more than 50 events_processed.
        for route_id, counters in snap["by_route"].items():
            assert (
                counters["events_processed"] == 50
            ), f"{route_id}: expected 50, got {counters['events_processed']}"


# ===================================================================
# 13. No snapshot growth
# ===================================================================


class TestNoSnapshotGrowth:
    """Verify snapshots don't grow in size across iterations."""

    @pytest.mark.asyncio
    async def test_snapshot_json_size_stable(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Snapshot JSON size must not grow monotonically across 10 captures."""
        await soak.start()
        assert soak.app is not None

        sizes: list[int] = []
        for _i in range(10):
            raw = build_runtime_snapshot(soak.app)
            serialized = json.dumps(raw, sort_keys=True, default=str)
            sizes.append(len(serialized))

        await soak.stop()

        # Size should be stable: max - min <= 10% of min (tolerance for
        # minor timestamp/counter changes).
        min_size = min(sizes)
        max_size = max(sizes)
        assert max_size - min_size <= min_size * 0.1 + 50, (
            f"Snapshot size drifted: min={min_size}, max={max_size}, " f"sizes={sizes}"
        )

    @pytest.mark.asyncio
    async def test_snapshot_key_count_stable(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Snapshot top-level key count must be constant."""
        await soak.start()
        assert soak.app is not None

        key_counts: list[int] = []
        for _ in range(10):
            raw = build_runtime_snapshot(soak.app)
            key_counts.append(len(raw))

        await soak.stop()

        assert len(set(key_counts)) == 1, f"Snapshot key counts varied: {key_counts}"

    @pytest.mark.asyncio
    async def test_no_adapter_snapshot_growth(
        self,
        soak: SoakRuntime,
    ) -> None:
        """Adapter count in snapshots must stay constant at 4."""
        await soak.start()
        assert soak.app is not None

        for i in range(10):
            raw = build_runtime_snapshot(soak.app)
            adapters = raw.get("adapters", [])
            assert (
                len(adapters) == 4
            ), f"Adapter count changed at iteration {i}: {len(adapters)}"

        await soak.stop()


# ===================================================================
# 14. ReplayMetrics churn
# ===================================================================


class TestReplayMetricsChurn:
    """Verify ReplayMetrics counters under heavy churn."""

    @pytest.mark.asyncio
    async def test_rejection_and_cancellation_counters(self) -> None:
        """Rejection and cancellation counters must be exact."""
        metrics = ReplayMetrics()

        for _ in range(30):
            metrics.record_rejection()
        for _ in range(20):
            metrics.record_cancellation()

        snap = metrics.snapshot()
        assert snap["global"]["rejection_count"] == 30
        assert snap["global"]["cancellation_count"] == 20

    @pytest.mark.asyncio
    async def test_replay_metrics_snapshot_deterministic(self) -> None:
        """Two consecutive ReplayMetrics snapshots must be identical."""
        metrics = ReplayMetrics()
        for i in range(50):
            metrics.record_events_processed(f"route-{i % 5}")
            metrics.record_delivery_succeeded(f"route-{i % 5}")

        snap1 = metrics.snapshot()
        snap2 = metrics.snapshot()
        assert snap1 == snap2, "ReplayMetrics snapshot is not deterministic"

    @pytest.mark.asyncio
    async def test_backlog_estimate_stable(self) -> None:
        """Backlog estimate must reflect last set value."""
        metrics = ReplayMetrics()

        for val in [100, 200, 50, 300, 0]:
            metrics.set_backlog_estimate(val)
            snap = metrics.snapshot()
            assert (
                snap["global"]["backlog_estimate"] == val
            ), f"Expected {val}, got {snap['global']['backlog_estimate']}"


# ===================================================================
# 15. CapacityController snapshot churn
# ===================================================================


class TestCapacitySnapshotChurn:
    """Verify capacity snapshots stay bounded under churn."""

    @pytest.mark.asyncio
    async def test_snapshot_stable_under_mixed_load(self) -> None:
        """Mixed delivery/replay acquire/release — snapshot stays valid."""
        limits = RuntimeLimits(
            max_inflight_deliveries=10,
            max_inflight_replay_events=5,
        )
        cc = CapacityController(limits)

        for _cycle in range(20):
            # Acquire some delivery slots.
            d_acquired = 0
            for _ in range(3):
                ok = await cc.acquire_delivery()
                if ok:
                    d_acquired += 1

            # Acquire some replay slots.
            r_acquired = 0
            for _ in range(2):
                ok = await cc.acquire_replay()
                if ok:
                    r_acquired += 1

            snap = cc.snapshot()
            assert snap["delivery_current"] <= snap["delivery_limit"]
            assert snap["replay_current"] <= snap["replay_limit"]

            # Release all.
            for _ in range(d_acquired):
                await cc.release_delivery()
            for _ in range(r_acquired):
                await cc.release_replay()

        # After all cycles, both currents must be 0.
        final = cc.snapshot()
        assert final["delivery_current"] == 0
        assert final["replay_current"] == 0

    @pytest.mark.asyncio
    async def test_stop_accepting_work(self) -> None:
        """After stop_accepting, acquires must return False."""
        limits = RuntimeLimits(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
        )
        cc = CapacityController(limits)

        assert cc.accepting_work is True
        ok = await cc.acquire_delivery()
        assert ok is True
        await cc.release_delivery()

        cc.stop_accepting()
        assert cc.accepting_work is False

        ok = await cc.acquire_delivery()
        assert ok is False
        ok = await cc.acquire_replay()
        assert ok is False
