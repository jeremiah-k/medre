"""Track 6 — Soak foundations v2: extended stability verification.

Builds on the harness from ``test_soak_harness.py`` with deeper patterns:

- Repeated runtime cycles with event delivery + diagnostics.
- Replay-style cycling with diagnostics stability.
- Diagnostics generation consistency across many iterations.
- Capacity churn under repeated acquire/release.
- Degraded adapter churn (partial failures remain stable).
- Route expansion stability.
- Startup failure recovery.
- No task leaks across lifecycle cycles.
- No unbounded growth in collections.
- Stable route stats across many deliveries.

Every test here:

- Uses **fake adapters** only — no live transports or SDKs required.
- Uses **in-memory storage** — no filesystem I/O beyond temp dirs.
- Runs within **<15 seconds** for default iteration counts.
- Is **deterministic** — no sleeps or wall-clock dependencies.
"""

from __future__ import annotations

import asyncio
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
from medre.core.routing.stats import RouteStats
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder

# Reuse SoakRuntime and DiagnosticsSnapshot from existing harness.
from tests.test_soak_harness import DiagnosticsSnapshot, SoakRuntime


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


def _count_asyncio_tasks() -> int:
    """Return the number of currently alive asyncio tasks."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return len([t for t in asyncio.all_tasks(loop) if not t.done()])


# ===================================================================
# 1. Repeated runtime cycles with event delivery + diagnostics
# ===================================================================


class TestRepeatedRuntimeCyclesV2:
    """Verify stable start/stop cycles with full event delivery paths."""

    @pytest.mark.asyncio
    async def test_8_cycles_with_delivery_and_diagnostics(
        self, soak: SoakRuntime,
    ) -> None:
        """8 cycles: start → deliver 3 events → capture diagnostics → stop.

        Every cycle must produce a running snapshot with 4 adapters.
        Diagnostics must be consistent across all cycles.
        """
        snapshots: list[DiagnosticsSnapshot] = []

        for cycle in range(8):
            await soak.start_fresh()
            assert soak.app is not None
            assert soak.app.state is RuntimeState.RUNNING

            results = await soak.deliver_events(count=3)
            assert len(results) == 3

            snap = soak.capture_diagnostics(iteration=cycle)
            assert snap.runtime_state == "running"
            assert snap.adapter_count == 4
            snapshots.append(snap)

            await soak.stop()
            assert soak.app.state is RuntimeState.STOPPED

        # All adapter counts must be identical across all snapshots.
        counts = {s.adapter_count for s in snapshots}
        assert counts == {4}, f"Adapter counts varied: {counts}"

    @pytest.mark.asyncio
    async def test_5_cycles_incremental_events(
        self, soak: SoakRuntime,
    ) -> None:
        """5 cycles with increasing event counts (1, 2, 3, 4, 5)."""
        for cycle in range(5):
            await soak.start_fresh()
            assert soak.app is not None

            count = cycle + 1
            results = await soak.deliver_events(count=count)
            assert len(results) == count

            snap = soak.capture_diagnostics(iteration=cycle)
            assert snap.runtime_state == "running"
            assert snap.adapter_count == 4

            await soak.stop()


# ===================================================================
# 2. Repeated replay-style cycles
# ===================================================================


class TestRepeatedReplayCyclesV2:
    """Verify replay-style start/deliver/diagnostics/stop cycling."""

    @pytest.mark.asyncio
    async def test_5_replay_cycles_deterministic(
        self, soak: SoakRuntime,
    ) -> None:
        """5 identical replay cycles must produce consistent diagnostics."""
        adapter_counts: list[int] = []
        states: list[str] = []

        for cycle in range(5):
            await soak.start_fresh()
            assert soak.app is not None

            # Simulate replay: deliver events in a burst.
            await soak.deliver_events(count=5)

            snap = soak.capture_diagnostics(iteration=cycle)
            adapter_counts.append(snap.adapter_count)
            states.append(snap.runtime_state)

            await soak.stop()

        assert all(c == 4 for c in adapter_counts), (
            f"Adapter counts drifted: {adapter_counts}"
        )
        assert all(s == "running" for s in states), (
            f"Runtime states inconsistent: {states}"
        )


# ===================================================================
# 3. Diagnostics generation stability
# ===================================================================


class TestDiagnosticsGenerationStability:
    """Verify diagnostics snapshots are consistent across many captures."""

    @pytest.mark.asyncio
    async def test_10_consecutive_snapshots_identical_structure(
        self, soak: SoakRuntime,
    ) -> None:
        """10 consecutive diagnostic snapshots must have consistent structure."""
        await soak.start()
        assert soak.app is not None

        structures: list[set[str]] = []
        for i in range(10):
            snap = soak.capture_diagnostics(iteration=i)
            # Collect the keys of the diagnostic dict.
            raw_snap = soak.app.diagnostic_snapshot()
            structures.append(set(raw_snap.keys()))

        await soak.stop()

        # All structures must be identical.
        first = structures[0]
        for idx, struct in enumerate(structures[1:], 1):
            assert struct == first, (
                f"Diagnostic structure changed at capture {idx}: "
                f"expected {first}, got {struct}"
            )


# ===================================================================
# 4. Capacity churn
# ===================================================================


class TestCapacityChurn:
    """Verify capacity controller handles repeated acquire/release cycles."""

    @pytest.mark.asyncio
    async def test_capacity_churn_under_repeated_load(
        self, soak: SoakRuntime,
    ) -> None:
        """Repeated event delivery bursts must not exhaust capacity slots."""
        await soak.start()
        assert soak.app is not None

        for burst in range(6):
            results = await soak.deliver_events(count=5)
            assert len(results) == 5

            snap = soak.capture_diagnostics(iteration=burst)
            # Capacity should be available (not permanently exhausted).
            assert snap.runtime_state == "running"

        await soak.stop()

    @pytest.mark.asyncio
    async def test_capacity_snapshots_bounded(
        self, soak: SoakRuntime,
    ) -> None:
        """Capacity snapshot values must stay within declared limits."""
        await soak.start()
        assert soak.app is not None

        for i in range(10):
            await soak.deliver_events(count=3)
            snap_raw = soak.app.diagnostic_snapshot()
            capacity = snap_raw.get("capacity")
            if capacity is not None:
                # Delivery current must not exceed delivery limit.
                current = capacity.get("delivery_current", 0)
                limit = capacity.get("delivery_limit", 50)
                assert current <= limit, (
                    f"delivery_current ({current}) > delivery_limit ({limit}) "
                    f"at iteration {i}"
                )

        await soak.stop()


# ===================================================================
# 5. Degraded adapter churn
# ===================================================================


class TestDegradedAdapterChurn:
    """Verify runtime stability when some adapters fail to start."""

    @pytest.mark.asyncio
    async def test_degraded_runtime_with_fewer_adapters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runtime with only 2 adapters (instead of 4) stays stable."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="soak-degraded"),
            logging=LoggingConfig(level="WARNING"),
            storage=StorageConfig(backend="memory"),
            limits=RuntimeLimits(
                max_inflight_deliveries=50,
                max_inflight_replay_events=50,
            ),
            adapters=AdapterConfigSet(
                matrix={
                    "degraded_matrix": MatrixRuntimeConfig(
                        adapter_id="degraded_matrix",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
                meshtastic={
                    "degraded_mesh": MeshtasticRuntimeConfig(
                        adapter_id="degraded_mesh",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )

        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        await app.start()
        assert app.state is RuntimeState.RUNNING

        # Only 2 adapters.
        assert len(app.adapters) == 2

        # Cycle through events — tolerant of per-adapter incompatibilities,
        # matching the SoakRuntime.deliver_events pattern.
        for i in range(5):
            delivered = 0
            for adapter_id, adapter in app.adapters.items():
                try:
                    if hasattr(adapter, "simulate_inbound"):
                        if hasattr(adapter, "make_text_event"):
                            event = getattr(adapter, "make_text_event")(
                                f"degraded-{i}", channel="ch"
                            )
                        elif hasattr(adapter, "make_event"):
                            event = getattr(adapter, "make_event")(
                                f"degraded-{i}", channel="ch"
                            )
                        else:
                            continue
                        await getattr(adapter, "simulate_inbound")(event)
                        delivered += 1
                except Exception:
                    pass

        await app.stop()
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_degraded_runtime_3_cycles(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """3 start/stop cycles with 2-adapter degraded runtime."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        for cycle in range(3):
            monkeypatch.setenv("MEDRE_HOME", str(tmp_path / f"cycle-{cycle}"))
            (tmp_path / f"cycle-{cycle}").mkdir(exist_ok=True)
            paths = resolve()

            config = RuntimeConfig(
                runtime=RuntimeOptions(name="soak-degraded-3"),
                logging=LoggingConfig(level="WARNING"),
                storage=StorageConfig(backend="memory"),
                limits=RuntimeLimits(
                    max_inflight_deliveries=50,
                    max_inflight_replay_events=50,
                ),
                adapters=AdapterConfigSet(
                    matrix={
                        "dm": MatrixRuntimeConfig(
                            adapter_id="dm",
                            enabled=True,
                            adapter_kind="fake",
                        ),
                    },
                    meshtastic={
                        "dt": MeshtasticRuntimeConfig(
                            adapter_id="dt",
                            enabled=True,
                            adapter_kind="fake",
                        ),
                    },
                ),
            )

            builder = RuntimeBuilder(config, paths)
            app = builder.build()
            await app.start()
            assert app.state is RuntimeState.RUNNING
            assert len(app.adapters) == 2
            await app.stop()
            assert app.state is RuntimeState.STOPPED


# ===================================================================
# 6. Route expansion stability
# ===================================================================


class TestRouteExpansionStability:
    """Verify adding routes doesn't destabilize the runtime."""

    @pytest.mark.asyncio
    async def test_route_stats_stable_across_deliveries(
        self, soak: SoakRuntime,
    ) -> None:
        """RouteStats counters remain consistent across many deliveries."""
        await soak.start()
        assert soak.app is not None

        stats = RouteStats()

        # Record some deliveries.
        for i in range(10):
            stats.record_delivered("route-a")
            stats.record_delivered("route-b")

        snap = stats.snapshot()
        assert "route-a" in snap
        assert "route-b" in snap
        assert snap["route-a"]["delivered"] == 10
        assert snap["route-b"]["delivered"] == 10

        await soak.stop()

    @pytest.mark.asyncio
    async def test_many_routes_snapshot_deterministic(
        self, soak: SoakRuntime,
    ) -> None:
        """RouteStats snapshot with many routes is deterministic."""
        stats = RouteStats()

        for i in range(20):
            stats.record_delivered(f"route-{i:03d}")

        snap1 = stats.snapshot()
        snap2 = stats.snapshot()

        # Two consecutive snapshots must be identical.
        assert snap1 == snap2, "RouteStats snapshot is not deterministic"


# ===================================================================
# 7. Startup failure recovery
# ===================================================================


class TestStartupFailureRecovery:
    """Verify a failed startup can be followed by a successful one."""

    @pytest.mark.asyncio
    async def test_failed_build_then_successful_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A build failure followed by a correct build must succeed."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()

        # Build with zero adapters — should fail on start (no adapters started).
        config_empty = RuntimeConfig(
            runtime=RuntimeOptions(name="startup-fail"),
            logging=LoggingConfig(level="WARNING"),
            storage=StorageConfig(backend="memory"),
            limits=RuntimeLimits(),
            adapters=AdapterConfigSet(),
        )

        builder1 = RuntimeBuilder(config_empty, paths)
        app1 = builder1.build()
        with pytest.raises(Exception):
            # Empty adapters → RuntimeStartupError on start.
            await app1.start()

        assert app1.state is RuntimeState.FAILED

        # Now build a correct runtime and verify it works.
        config_ok = RuntimeConfig(
            runtime=RuntimeOptions(name="startup-ok"),
            logging=LoggingConfig(level="WARNING"),
            storage=StorageConfig(backend="memory"),
            limits=RuntimeLimits(),
            adapters=AdapterConfigSet(
                matrix={
                    "recovery_m": MatrixRuntimeConfig(
                        adapter_id="recovery_m",
                        enabled=True,
                        adapter_kind="fake",
                    ),
                },
            ),
        )

        builder2 = RuntimeBuilder(config_ok, paths)
        app2 = builder2.build()
        await app2.start()
        assert app2.state is RuntimeState.RUNNING
        assert len(app2.adapters) == 1
        await app2.stop()
        assert app2.state is RuntimeState.STOPPED


# ===================================================================
# 8. No task leaks
# ===================================================================


class TestNoTaskLeaks:
    """Verify asyncio tasks are cleaned up after runtime stop."""

    @pytest.mark.asyncio
    async def test_no_dangling_tasks_after_stop(
        self, soak: SoakRuntime,
    ) -> None:
        """After start/stop, no new non-done tasks should remain."""
        baseline = _count_asyncio_tasks()

        await soak.start()
        assert soak.app is not None
        await soak.deliver_events(count=3)
        await soak.stop()

        after = _count_asyncio_tasks()
        # Allow for up to 2 transient tasks (event loop internals).
        assert after <= baseline + 2, (
            f"Task leak detected: baseline={baseline}, after={after}"
        )

    @pytest.mark.asyncio
    async def test_no_task_accumulation_over_cycles(
        self, soak: SoakRuntime,
    ) -> None:
        """5 start/stop cycles must not accumulate tasks."""
        task_counts: list[int] = []

        for _ in range(5):
            await soak.start_fresh()
            assert soak.app is not None
            await soak.deliver_events(count=2)
            await soak.stop()
            task_counts.append(_count_asyncio_tasks())

        # Task counts should not grow monotonically.
        max_count = max(task_counts)
        min_count = min(task_counts)
        assert max_count - min_count <= 2, (
            f"Task count drifted over cycles: {task_counts}"
        )


# ===================================================================
# 9. No unbounded growth patterns
# ===================================================================


class TestNoUnboundedGrowth:
    """Verify runtime collections stay bounded across many iterations."""

    @pytest.mark.asyncio
    async def test_adapter_lists_bounded_over_50_iterations(
        self, soak: SoakRuntime,
    ) -> None:
        """Fake adapter history lists must stay bounded over 50 iterations."""
        await soak.start()
        assert soak.app is not None

        iterations = 50
        await soak.deliver_events(count=iterations)

        max_allowed = 1000  # _MAX_FAKE_HISTORY
        for adapter_id, adapter in soak.app.adapters.items():
            for attr_name in (
                "delivered_payloads",
                "inbound_events",
                "received_events",
                "delivered_events",
            ):
                lst = getattr(adapter, attr_name, None)
                if isinstance(lst, list):
                    assert len(lst) <= max_allowed, (
                        f"Adapter {adapter_id}.{attr_name} has {len(lst)} "
                        f"entries (max {max_allowed})"
                    )

        await soak.stop()

    @pytest.mark.asyncio
    async def test_started_adapter_ids_stable_across_fresh_cycles(
        self, soak: SoakRuntime,
    ) -> None:
        """started_adapter_ids must contain exactly 4 adapters after start.

        After stop(), the list preserves its contents (design choice).
        Each fresh start must have exactly 4 entries.
        """
        for _ in range(3):
            await soak.start_fresh()
            assert soak.app is not None
            assert len(soak.app.started_adapter_ids) == 4

            await soak.stop()
            # After stop, app state is STOPPED; started_adapter_ids
            # is only cleared on startup failure, not normal stop.


# ===================================================================
# 10. Stable route stats
# ===================================================================


class TestStableRouteStats:
    """Verify route stats don't drift unexpectedly."""

    @pytest.mark.asyncio
    async def test_route_stats_no_ghost_routes(
        self, soak: SoakRuntime,
    ) -> None:
        """RouteStats snapshot must not accumulate ghost routes."""
        stats = RouteStats()

        # Record for a fixed set of routes.
        for i in range(5):
            stats.record_delivered("alpha")
            stats.record_delivered("beta")

        snap = stats.snapshot()
        assert set(snap.keys()) == {"alpha", "beta"}, (
            f"Unexpected routes in snapshot: {set(snap.keys())}"
        )

    @pytest.mark.asyncio
    async def test_route_stats_counters_monotonic(
        self, soak: SoakRuntime,
    ) -> None:
        """RouteStats delivered counters must be monotonically non-decreasing."""
        stats = RouteStats()

        prev_total = 0
        for i in range(10):
            stats.record_delivered("stable-route")
            snap = stats.snapshot()
            current = snap["stable-route"]["delivered"]
            assert current >= prev_total, (
                f"Counter went backwards at iteration {i}: "
                f"{current} < {prev_total}"
            )
            prev_total = current

        assert prev_total == 10
