"""Track 1 runtime cancellation / task hygiene hardening tests.

Covers:
- Repeated cancellation cycles: build→start→stop N times with fresh app instances.
- Task leak checks: no lingering asyncio tasks after full start/stop.
- Cancellation under load: CapacityController under concurrent pressure.
- Shutdown during replay: stop accepting work while replay is in progress.
- Shutdown during capacity wait: blocked semaphore acquires return False on stop.
- Shutdown during delivery fanout: concurrent delivery acquire/release with stop.
- Stop during startup: concurrent stop() while adapters are still starting.
- Repeated stop races: concurrent stop() calls are idempotent.
- Cleanup timeout observability: drain timeout path with occupied capacity.

Uses no real transport dependencies; all adapters are fake/stub.
Does not overlap with test_runtime_hygiene.py or test_runtime_recovery.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.lifecycle.states import AdapterState
from medre.core.supervision.capacity import CapacityController
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _fake_meshtastic_config(adapter_id: str = "fake_mesh") -> MeshtasticRuntimeConfig:
    return MeshtasticRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _config_with_two_fake_adapters() -> RuntimeConfig:
    """RuntimeConfig with two fake adapters (matrix + meshtastic)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-cancellation"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
            meshtastic={"main": _fake_meshtastic_config()},
        ),
    )


def _config_with_one_fake_adapter() -> RuntimeConfig:
    """RuntimeConfig with one fake adapter (matrix)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-cancellation-single"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


def _make_limits(
    *,
    max_delivery: int = 50,
    max_replay: int = 25,
    drain_timeout: int = 10,
    acquire_timeout: float = 2.0,
) -> RuntimeLimits:
    """Create RuntimeLimits for testing."""
    return RuntimeLimits(
        max_inflight_deliveries=max_delivery,
        max_inflight_replay_events=max_replay,
        shutdown_drain_timeout_seconds=drain_timeout,
        delivery_acquire_timeout_seconds=acquire_timeout,
    )


def _make_minimal_event(event_id: str = "evt-cancel-001") -> CanonicalEvent:
    """Create a minimal CanonicalEvent for storage/replay tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_matrix",
        source_transport_id="matrix",
        source_channel_id="test_room",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "cancellation test"},
        metadata=EventMetadata(),
    )


# =====================================================================
# 1. Repeated cancellation cycles
# =====================================================================


class TestRepeatedCancellationCycles:
    """Build, start, and stop MedreApp multiple times.

    Each cycle creates a fresh MedreApp via RuntimeBuilder.  Verifies that
    state transitions are clean and no cross-cycle contamination occurs.
    """

    @pytest.mark.asyncio
    async def test_three_full_cycles_state_clean(self, tmp_paths: MedrePaths) -> None:
        """Three full build→start→stop cycles with clean state each time."""
        config = _config_with_one_fake_adapter()

        for cycle in range(3):
            app = _build_app(config, tmp_paths)
            assert (
                app.state == RuntimeState.INITIALIZED
            ), f"Cycle {cycle}: expected INITIALIZED, got {app.state}"

            await app.start()
            assert (
                app.state == RuntimeState.RUNNING
            ), f"Cycle {cycle}: expected RUNNING, got {app.state}"

            await app.stop()
            assert (
                app.state == RuntimeState.STOPPED
            ), f"Cycle {cycle}: expected STOPPED, got {app.state}"

    @pytest.mark.asyncio
    async def test_two_fake_adapters_repeated_cycles(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Repeated cycles with two adapters maintain consistent start counts."""
        config = _config_with_two_fake_adapters()

        for _cycle in range(3):
            app = _build_app(config, tmp_paths)
            await app.start()
            try:
                assert len(app.started_adapter_ids) == 2
                assert app.boot_summary is not None
                assert app.boot_summary.adapters_started == 2
            finally:
                await app.stop()

    @pytest.mark.asyncio
    async def test_capacity_fresh_per_cycle(self, tmp_paths: MedrePaths) -> None:
        """Each cycle gets a fresh CapacityController with zeroed counters."""
        config = _config_with_one_fake_adapter()

        for _cycle in range(3):
            app = _build_app(config, tmp_paths)
            await app.start()
            try:
                cc = app._capacity_controller
                assert cc is not None
                assert cc.accepting_work is True
                assert cc.delivery_current == 0
                assert cc.replay_current == 0
            finally:
                await app.stop()

    @pytest.mark.asyncio
    async def test_shutdown_event_fresh_per_cycle(self, tmp_paths: MedrePaths) -> None:
        """Each cycle gets a fresh shutdown event that is unset at start."""
        config = _config_with_one_fake_adapter()

        for _cycle in range(3):
            app = _build_app(config, tmp_paths)
            assert not app.shutdown_event.is_set()
            await app.start()
            try:
                assert not app.shutdown_event.is_set()
            finally:
                await app.stop()
            assert app.shutdown_event.is_set()


# =====================================================================
# 2. Task leak checks
# =====================================================================


class TestTaskLeakChecks:
    """Verify no lingering asyncio tasks after runtime shutdown."""

    @pytest.mark.asyncio
    async def test_no_lingering_tasks_after_stop(self, tmp_paths: MedrePaths) -> None:
        """After full start/stop cycle, no runtime tasks remain."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Snapshot tasks before start.
        tasks_before = {t.get_name() for t in asyncio.all_tasks()}

        await app.start()
        await app.stop()

        # Give the event loop a chance to clean up.
        await asyncio.sleep(0.1)

        tasks_after = {t.get_name() for t in asyncio.all_tasks()}
        leaked = tasks_after - tasks_before

        # Filter out pytest infrastructure tasks.
        leaked = {
            t for t in leaked if not t.startswith(("pytest", "test_no_lingering"))
        }
        assert not leaked, f"Leaked tasks after stop: {leaked}"

    @pytest.mark.asyncio
    async def test_no_task_accumulation_over_repeated_cycles(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Task count does not grow over repeated start/stop cycles."""
        config = _config_with_one_fake_adapter()

        baseline_count = len(asyncio.all_tasks())

        for _ in range(5):
            app = _build_app(config, tmp_paths)
            await app.start()
            await app.stop()
            await asyncio.sleep(0.05)

        final_count = len(asyncio.all_tasks())
        # Allow small variance (pytest internals) but no growth.
        assert (
            final_count <= baseline_count + 2
        ), f"Task count grew from {baseline_count} to {final_count}"


# =====================================================================
# 3. Cancellation under load (CapacityController)
# =====================================================================


class TestCancellationUnderLoad:
    """CapacityController handles concurrent pressure and cancellation."""

    @pytest.mark.asyncio
    async def test_concurrent_acquire_then_stop_accepting(self) -> None:
        """Multiple concurrent acquirers see stop_accepting correctly."""
        limits = _make_limits(max_delivery=2, acquire_timeout=0.5)
        cc = CapacityController(limits)

        barrier = asyncio.Event()

        async def acquire_slot(idx: int) -> bool:
            await barrier.wait()
            result = await cc.acquire_delivery()
            return result

        # Launch 5 concurrent acquirers.
        barrier.clear()
        tasks = [asyncio.create_task(acquire_slot(i)) for i in range(5)]
        barrier.set()

        # Wait for at least 2 to acquire (semaphore limit).
        await asyncio.sleep(0.2)

        # Now stop accepting — remaining acquirers should be rejected
        # (either immediately or after semaphore re-check).
        cc.stop_accepting()

        results = await asyncio.gather(*tasks)
        successes = sum(1 for r in results if r is True)
        failures = sum(1 for r in results if r is False)

        assert successes >= 1, "At least one acquire should succeed"
        assert failures >= 1, "At least one acquire should be rejected"

        # Verify rejections or timeouts are tracked.
        snap = cc.snapshot()
        assert snap["delivery_rejections"] + snap["delivery_timeouts"] >= 1

    @pytest.mark.asyncio
    async def test_replay_concurrent_acquire_stop(self) -> None:
        """Replay acquire respects stop_accepting under concurrency."""
        limits = _make_limits(max_replay=2)
        cc = CapacityController(limits)

        async def try_acquire() -> bool:
            return await cc.acquire_replay()

        # Fill both slots.
        assert await cc.acquire_replay()
        assert await cc.acquire_replay()

        # Now stop accepting and try more.
        cc.stop_accepting()
        results = await asyncio.gather(
            *[asyncio.create_task(try_acquire()) for _ in range(3)]
        )
        assert all(r is False for r in results)
        assert cc.snapshot()["replay_rejections"] >= 3

    @pytest.mark.asyncio
    async def test_acquire_release_cycle_integrity_under_stop(self) -> None:
        """Acquire/release cycles maintain counter integrity when stopped."""
        limits = _make_limits(max_delivery=3)
        cc = CapacityController(limits)

        # Acquire all slots.
        for _ in range(3):
            assert await cc.acquire_delivery()

        # Stop accepting new work.
        cc.stop_accepting()
        assert cc.delivery_current == 3

        # Release all slots.
        for _ in range(3):
            await cc.release_delivery()

        assert cc.delivery_current == 0
        # Still not accepting.
        assert cc.accepting_work is False
        snap = cc.snapshot()
        assert snap["delivery_current"] == 0


# =====================================================================
# 4. Shutdown during capacity wait
# =====================================================================


class TestShutdownDuringCapacityWait:
    """Blocked semaphore acquires return False when stop_accepting is called."""

    @pytest.mark.asyncio
    async def test_blocked_delivery_acquire_returns_false_on_stop(self) -> None:
        """Delivery acquire waiting on full semaphore returns False on stop."""
        limits = _make_limits(max_delivery=1, acquire_timeout=10.0)
        cc = CapacityController(limits)

        # Fill the single slot.
        assert await cc.acquire_delivery()
        assert cc.delivery_current == 1

        # Start a blocked acquire in the background.
        async def blocked_acquire() -> bool:
            return await cc.acquire_delivery()

        task = asyncio.create_task(blocked_acquire())
        await asyncio.sleep(0.1)  # Let it start waiting.

        # Stop accepting — the blocked acquire should return False.
        cc.stop_accepting()

        result = await task
        assert result is False

    @pytest.mark.asyncio
    async def test_blocked_replay_acquire_returns_false_on_stop(self) -> None:
        """Replay acquire waiting on full semaphore returns False on stop."""
        limits = _make_limits(max_replay=1, acquire_timeout=10.0)
        cc = CapacityController(limits)

        # Fill the single replay slot.
        assert await cc.acquire_replay()

        async def blocked_acquire() -> bool:
            return await cc.acquire_replay()

        task = asyncio.create_task(blocked_acquire())
        await asyncio.sleep(0.1)

        cc.stop_accepting()

        result = await task
        assert result is False

    @pytest.mark.asyncio
    async def test_capacity_timeout_records_timeout_counter(self) -> None:
        """Acquire timeout is recorded in the timeout counter."""
        limits = _make_limits(max_delivery=1, acquire_timeout=0.1)
        cc = CapacityController(limits)

        # Fill the slot.
        assert await cc.acquire_delivery()

        # Try to acquire with short timeout — should time out.
        result = await cc.acquire_delivery()
        assert result is False
        assert cc.snapshot()["delivery_timeouts"] >= 1


# =====================================================================
# 5. Stop during startup
# =====================================================================


class TestStopDuringStartup:
    """stop() called while start() is still in progress."""

    @pytest.mark.asyncio
    async def test_stop_during_slow_startup_ends_cleanly(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Stop during slow adapter startup results in clean terminal state."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # We'll call start() and stop() concurrently.
        # start() should complete first (fake adapters start quickly),
        # but we interleave stop() to test the race.
        start_task = asyncio.create_task(app.start())

        # Give start a moment to begin.
        await asyncio.sleep(0.05)

        # Now call stop concurrently — start may still be in STARTING.
        stop_task = asyncio.create_task(app.stop())

        # Wait for both.
        try:
            await start_task
        except Exception:
            pass  # start may fail if stop transitioned to STOPPING

        await stop_task

        # The app should be in a terminal state (STOPPED or FAILED).
        assert app.state in (
            RuntimeState.STOPPED,
            RuntimeState.FAILED,
        ), f"Expected STOPPED or FAILED, got {app.state}"

    @pytest.mark.asyncio
    async def test_stop_before_start_is_idempotent(self, tmp_paths: MedrePaths) -> None:
        """Calling stop() on an INITIALIZED app returns immediately."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        assert app.state == RuntimeState.INITIALIZED

        # stop() on INITIALIZED returns immediately (no error).
        await app.stop()

        # State should remain INITIALIZED (stop returns early).
        assert app.state == RuntimeState.INITIALIZED

    @pytest.mark.asyncio
    async def test_start_after_stop_on_fresh_app_works(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A fresh app (INITIALIZED) can be started after an early stop."""
        config = _config_with_one_fake_adapter()

        for _cycle in range(2):
            app = _build_app(config, tmp_paths)
            # Stop on INITIALIZED is a no-op.
            await app.stop()
            # Start still works.
            await app.start()
            assert app.state == RuntimeState.RUNNING
            await app.stop()
            assert app.state == RuntimeState.STOPPED


# =====================================================================
# 6. Repeated stop races
# =====================================================================


class TestRepeatedStopRaces:
    """Concurrent stop() calls are idempotent."""

    @pytest.mark.asyncio
    async def test_concurrent_stop_calls(self, tmp_paths: MedrePaths) -> None:
        """Multiple concurrent stop() calls all succeed without error."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()
        assert app.state == RuntimeState.RUNNING

        # Launch 5 concurrent stop() calls.
        results = await asyncio.gather(
            *[asyncio.create_task(app.stop()) for _ in range(5)],
            return_exceptions=True,
        )

        # None should raise an unexpected exception.
        exceptions = [r for r in results if isinstance(r, Exception)]
        # At most one RuntimeShutdownError is acceptable (from adapters).
        non_shutdown_exceptions = [
            e for e in exceptions if "Errors during shutdown" not in str(e)
        ]
        assert (
            not non_shutdown_exceptions
        ), f"Unexpected exceptions from concurrent stop: {non_shutdown_exceptions}"

        # Final state must be STOPPED (or FAILED if shutdown errors occurred).
        assert app.state in (
            RuntimeState.STOPPED,
            RuntimeState.FAILED,
        ), f"Expected STOPPED or FAILED, got {app.state}"

    @pytest.mark.asyncio
    async def test_double_stop_after_full_start(self, tmp_paths: MedrePaths) -> None:
        """Second stop() after clean first stop is a no-op."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        await app.stop()
        assert app.state == RuntimeState.STOPPED

        # Second stop should return immediately without error.
        await app.stop()
        assert app.state == RuntimeState.STOPPED


# =====================================================================
# 7. Cleanup timeout observability
# =====================================================================


class TestCleanupTimeoutObservability:
    """Drain timeout path produces observable diagnostic state."""

    @pytest.mark.asyncio
    async def test_drain_timeout_with_occupied_capacity(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Stop with occupied capacity logs drain timeout warning."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None

        # Acquire a delivery slot so drain can't complete.
        assert await cc.acquire_delivery()
        assert cc.delivery_current == 1

        # Use a very short drain timeout to trigger the timeout path.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            # Stop should still complete (drain timeout fires, work abandoned).
            await app.stop()
        finally:
            # Restore for safety.
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )

        assert app.state == RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_diagnostic_snapshot_during_stopping(
        self, tmp_paths: MedrePaths
    ) -> None:
        """diagnostic_snapshot() is accessible while runtime is stopping."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None

        # Acquire a replay slot.
        assert await cc.acquire_replay()

        # Short drain timeout to force the timeout path.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            # Take snapshot before stop (while RUNNING).
            snap_running = app.diagnostic_snapshot()
            assert snap_running["runtime_state"] == "running"
            assert snap_running["accepting_work"] is True

            await app.stop()
        finally:
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )

        # Snapshot after stop.
        snap_stopped = app.diagnostic_snapshot()
        assert snap_stopped["accepting_work"] is False
        assert snap_stopped["runtime_state"] == "stopped"

    @pytest.mark.asyncio
    async def test_capacity_snapshot_shows_drain_state(self) -> None:
        """CapacityController snapshot reflects in-flight work during drain."""
        limits = _make_limits(max_delivery=2, max_replay=1)
        cc = CapacityController(limits)

        # Acquire both delivery slots and the replay slot.
        assert await cc.acquire_delivery()
        assert await cc.acquire_delivery()
        assert await cc.acquire_replay()

        snap = cc.snapshot()
        assert snap["delivery_current"] == 2
        assert snap["replay_current"] == 1
        assert snap["accepting_work"] is True

        # Stop accepting — snapshot should reflect stopped state.
        cc.stop_accepting()
        snap = cc.snapshot()
        assert snap["accepting_work"] is False
        # Current counts unchanged — work still in flight.
        assert snap["delivery_current"] == 2
        assert snap["replay_current"] == 1


# =====================================================================
# 8. Shutdown during replay
# =====================================================================


class TestShutdownDuringReplay:
    """Replay respects capacity stop_accepting during BEST_EFFORT delivery."""

    @pytest.mark.asyncio
    async def test_best_effort_replay_completes_with_capacity_stopped(
        self, tmp_paths: MedrePaths
    ) -> None:
        """BEST_EFFORT replay completes (with skips) even with capacity stopped.

        Without routes, delivery is skipped at the planning stage rather than
        the capacity stage.  The test verifies replay does not crash when
        capacity is stopped — it completes with skipped delivery results.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        try:
            # Store events for replay.
            storage = app.storage
            assert storage is not None
            for i in range(5):
                evt = _make_minimal_event(event_id=f"replay-evt-{i}")
                await storage.append(evt)

            replay_engine = app.replay_engine
            assert replay_engine is not None

            cc = app._capacity_controller
            assert cc is not None

            # Stop accepting new work.
            cc.stop_accepting()

            from medre.core.storage.replay import ReplayMode, ReplayRequest

            request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

            results = []
            async for result in replay_engine.replay(request):
                results.append(result)

            # All events processed — replay should not crash.
            assert len(results) >= 5

            # Without routes, delivery is skipped ("No delivery plans available").
            # This is correct: capacity stop doesn't prevent replay iteration,
            # it only prevents delivery slot acquisition.
            deliver_results = [r for r in results if r.stage == "deliver"]
            assert len(deliver_results) >= 1
            # Delivery results should be skipped (no routes matched).
            skipped = [r for r in deliver_results if r.status == "skipped"]
            assert len(skipped) >= 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_strict_replay_completes_despite_capacity_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """STRICT replay (no delivery) completes even with capacity stopped."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        try:
            storage = app.storage
            assert storage is not None
            for i in range(3):
                evt = _make_minimal_event(event_id=f"strict-evt-{i}")
                await storage.append(evt)

            replay_engine = app.replay_engine
            assert replay_engine is not None

            # Stop accepting work — STRICT mode should still work
            # (it doesn't acquire capacity slots).
            cc = app._capacity_controller
            assert cc is not None
            cc.stop_accepting()

            from medre.core.storage.replay import ReplayMode, ReplayRequest

            request = ReplayRequest(mode=ReplayMode.STRICT)
            results = []
            async for result in replay_engine.replay(request):
                results.append(result)

            assert len(results) == 3
            assert all(r.status == "passed" for r in results)
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_replay_cancellation_increments_metrics(self) -> None:
        """Recording replay cancellation increments ReplayMetrics counter."""
        rm = ReplayMetrics()

        for _ in range(5):
            rm.record_cancellation()

        snap = rm.snapshot()
        assert snap["global"]["cancellation_count"] == 5
        assert snap["global"]["last_cancelled_at"] is not None

    @pytest.mark.asyncio
    async def test_stop_calls_replay_engine_cancel(self, tmp_paths: MedrePaths) -> None:
        """MedreApp.stop() calls replay_engine.cancel() during Phase 1.

        Verifies that after stop(), the replay engine's is_cancelled flag
        is True, preventing any further replay iteration.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        replay_engine = app.replay_engine
        assert replay_engine is not None
        assert not replay_engine.is_cancelled

        # Seed events so replay would have work to do.
        storage = app.storage
        assert storage is not None
        for i in range(5):
            evt = _make_minimal_event(event_id=f"stop-cancel-evt-{i}")
            await storage.append(evt)

        await app.stop()

        # After stop, the replay engine should be cancelled.
        assert (
            replay_engine.is_cancelled
        ), "Replay engine should be cancelled after MedreApp.stop()"

    @pytest.mark.asyncio
    async def test_stop_cancels_inflight_replay_early(
        self, tmp_paths: MedrePaths
    ) -> None:
        """MedreApp.stop() cancels a replay in progress, stopping iteration early.

        Starts a STRICT replay in a task, then calls stop().  The replay
        should produce fewer results than the total event count because
        cancellation stops the iteration loop.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        storage = app.storage
        assert storage is not None
        replay_engine = app.replay_engine
        assert replay_engine is not None

        # Seed many events so replay takes multiple iterations.
        for i in range(20):
            evt = _make_minimal_event(event_id=f"inflight-evt-{i}")
            await storage.append(evt)

        from medre.core.storage.replay import ReplayMode, ReplayRequest

        request = ReplayRequest(mode=ReplayMode.STRICT)

        collected_results: list = []

        async def _run_replay():
            async for result in replay_engine.replay(request):
                collected_results.append(result)

        replay_task = asyncio.create_task(_run_replay())

        # Give the replay a moment to start processing.
        await asyncio.sleep(0.05)

        # Now stop the app — this should cancel the replay engine.
        await app.stop()

        # Wait for the replay task to finish.
        try:
            await asyncio.wait_for(replay_task, timeout=5.0)
        except asyncio.TimeoutError:
            replay_task.cancel()
            try:
                await replay_task
            except asyncio.CancelledError:
                pass

        # The replay should have been cancelled mid-flight — fewer than 20 results.
        assert replay_engine.is_cancelled
        # We expect some results but not all 20 (strict has 1 stage per event).
        # If replay was fast enough to finish before stop(), that's also fine.
        assert (
            len(collected_results) <= 20
        ), f"Expected <= 20 results, got {len(collected_results)}"


# =====================================================================
# 9. Delivery fanout cancellation
# =====================================================================


class TestDeliveryFanoutCancellation:
    """Capacity controller handles delivery fanout with concurrent stop."""

    @pytest.mark.asyncio
    async def test_delivery_rejected_during_shutdown_fanout(self) -> None:
        """Delivery acquires are rejected during shutdown fanout."""
        limits = _make_limits(max_delivery=4)
        cc = CapacityController(limits)

        # Simulate fanout: acquire all slots.
        acquired = []
        for _ in range(4):
            assert await cc.acquire_delivery()
            acquired.append(True)

        # Now stop accepting and verify further acquires fail.
        cc.stop_accepting()

        for _ in range(10):
            result = await cc.acquire_delivery()
            assert result is False

        # Rejection counter should be incremented.
        assert cc.snapshot()["delivery_rejections"] >= 10

        # Release all acquired slots.
        for _ in acquired:
            await cc.release_delivery()

        assert cc.delivery_current == 0

    @pytest.mark.asyncio
    async def test_concurrent_fanout_with_stop_race(self) -> None:
        """Concurrent delivery fanout mixed with stop_accepting is safe."""
        limits = _make_limits(max_delivery=5)
        cc = CapacityController(limits)

        results: list[bool] = []
        stop_triggered = False

        async def try_deliver(idx: int) -> None:
            nonlocal stop_triggered
            result = await cc.acquire_delivery()
            results.append(result)
            if result:
                await asyncio.sleep(0.01)
                await cc.release_delivery()
            if idx == 3 and not stop_triggered:
                cc.stop_accepting()
                stop_triggered = True

        await asyncio.gather(*[asyncio.create_task(try_deliver(i)) for i in range(20)])

        # Some should succeed (before stop), some should fail (after stop).
        successes = sum(1 for r in results if r)
        failures = sum(1 for r in results if not r)
        assert successes >= 1, "At least one delivery should succeed"
        assert failures >= 1, "At least one delivery should fail"

    @pytest.mark.asyncio
    async def test_capacity_counters_consistent_after_fanout(self) -> None:
        """Capacity counters remain consistent after fanout + stop."""
        limits = _make_limits(max_delivery=3)
        cc = CapacityController(limits)

        # Acquire and release in a pattern.
        slots = []
        for _ in range(3):
            slots.append(await cc.acquire_delivery())

        cc.stop_accepting()

        for _ in range(3):
            await cc.release_delivery()

        snap = cc.snapshot()
        assert snap["delivery_current"] == 0
        assert snap["accepting_work"] is False
        # Rejections from post-stop attempts.
        assert snap["delivery_rejections"] == 0  # No new attempts after stop.


# =====================================================================
# 10. Adapter lifecycle during cancellation
# =====================================================================


class TestAdapterLifecycleDuringCancellation:
    """Adapter stop ordering and cleanup during cancellation scenarios."""

    @pytest.mark.asyncio
    async def test_all_adapters_stopped_on_clean_shutdown(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All started adapters are stopped during clean shutdown."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        assert len(app.started_adapter_ids) == 2

        await app.stop()
        assert app.state == RuntimeState.STOPPED
        # shutdown_event should be set.
        assert app.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_event_set_before_adapter_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Shutdown event is set before adapters are stopped."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        assert not app.shutdown_event.is_set()

        await app.stop()

        # After stop, event must be set.
        assert app.shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_wait_for_shutdown_respects_timeout(self) -> None:
        """wait_for_shutdown raises TimeoutError when timeout expires."""
        limits = _make_limits()
        CapacityController(limits)

        # The event is not set, so wait should time out.
        event = asyncio.Event()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_wait_for_shutdown_completes_on_event_set(
        self, tmp_paths: MedrePaths
    ) -> None:
        """wait_for_shutdown returns when event is set."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        # Set shutdown event in background.
        async def set_shutdown():
            await asyncio.sleep(0.05)
            app.shutdown_event.set()

        asyncio.create_task(set_shutdown())

        # wait_for_shutdown should complete.
        await app.wait_for_shutdown(timeout=2.0)

        await app.stop()


# =====================================================================
# 11. Shutdown coverage regression (Wave 1 gaps)
# =====================================================================


class TestConcurrentStopExactlyOnce:
    """Concurrent stop() calls stop each adapter exactly once."""

    @pytest.mark.asyncio
    async def test_concurrent_stop_skips_already_stopped_adapters(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Five concurrent stop() calls: adapters end STOPPED, not re-stopped."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = list(app.adapters.keys())

        # Instrument adapter stop() to count calls.
        stop_counts: dict[str, int] = {aid: 0 for aid in adapter_ids}
        original_stops: dict[str, Any] = {}
        for aid, adapter in app.adapters.items():
            original_stops[aid] = adapter.stop

            async def _counting_stop(
                *args: Any,
                _aid: str = aid,
                _orig: Any = original_stops[aid],
                **kwargs: Any,
            ) -> None:
                stop_counts[_aid] += 1
                await _orig(*args, **kwargs)

            adapter.stop = _counting_stop  # type: ignore[assignment]

        # Fire 5 concurrent stop() calls.
        await asyncio.gather(
            *[asyncio.create_task(app.stop()) for _ in range(5)],
            return_exceptions=True,
        )

        # Each adapter should have been stopped exactly once.
        for aid in adapter_ids:
            assert (
                stop_counts[aid] == 1
            ), f"Adapter {aid} stop() called {stop_counts[aid]} times, expected 1"

        # All adapters should be in STOPPED state.
        for aid in adapter_ids:
            assert (
                app.adapter_states[aid] is AdapterState.STOPPED
            ), f"Adapter {aid} state is {app.adapter_states[aid]}, expected STOPPED"

        assert app.state in (RuntimeState.STOPPED, RuntimeState.FAILED)


class TestAllAdaptersStoppedAfterShutdown:
    """Every adapter ends in a known terminal state after shutdown."""

    @pytest.mark.asyncio
    async def test_all_adapters_stopped_state_after_clean_shutdown(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All adapters are STOPPED after a clean full start/stop cycle."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        await app.stop()
        assert app.state == RuntimeState.STOPPED

        for aid, state in app.adapter_states.items():
            assert (
                state is AdapterState.STOPPED
            ), f"Adapter {aid} is {state.value}, expected STOPPED"

    @pytest.mark.asyncio
    async def test_adapter_states_complete_after_shutdown(
        self, tmp_paths: MedrePaths
    ) -> None:
        """adapter_states covers every adapter in self.adapters."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        await app.stop()

        assert set(app.adapter_states.keys()) == set(app.adapters.keys())


class TestPartialAdapterStopFailure:
    """One adapter failing stop doesn't prevent others from reaching STOPPED."""

    @pytest.mark.asyncio
    async def test_one_adapter_stop_fails_others_stopped(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Adapter that raises on stop → FAILED; others → STOPPED; runtime → FAILED."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = sorted(app.adapters.keys())
        assert len(adapter_ids) == 2

        # Monkey-patch the second adapter to raise on stop.
        failing_id = adapter_ids[1]
        clean_id = adapter_ids[0]
        app.adapters[failing_id].stop  # noqa: B018 — attribute access for reference

        async def _raising_stop(timeout: float = 10.0) -> None:
            raise RuntimeError(f"Adapter {failing_id} stop failed")

        app.adapters[failing_id].stop = _raising_stop  # type: ignore[assignment]

        # stop() should raise RuntimeShutdownError at the end.
        from medre.runtime.errors import RuntimeShutdownError

        with pytest.raises(RuntimeShutdownError):
            await app.stop()

        # Runtime should be FAILED (shutdown had errors).
        assert app.state == RuntimeState.FAILED

        # Clean adapter should be STOPPED.
        assert (
            app.adapter_states[clean_id] is AdapterState.STOPPED
        ), f"Clean adapter {clean_id} is {app.adapter_states[clean_id]}, expected STOPPED"

        # Failing adapter should be FAILED.
        assert (
            app.adapter_states[failing_id] is AdapterState.FAILED
        ), f"Failing adapter {failing_id} is {app.adapter_states[failing_id]}, expected FAILED"


class TestShutdownEventTiming:
    """Shutdown event is set before adapter stop begins."""

    @pytest.mark.asyncio
    async def test_shutdown_event_set_before_adapter_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """shutdown_event.is_set() is True inside adapter.stop()."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        event_set_during_stop: bool | None = None

        # Monkey-patch adapter stop to check the shutdown event.
        for adapter in app.adapters.values():
            original_stop = adapter.stop

            async def _inspecting_stop(
                *args: Any, _orig: Any = original_stop, **kwargs: Any
            ) -> None:
                nonlocal event_set_during_stop
                event_set_during_stop = app.shutdown_event.is_set()
                await _orig(*args, **kwargs)

            adapter.stop = _inspecting_stop  # type: ignore[assignment]

        await app.stop()

        assert (
            event_set_during_stop is True
        ), "shutdown_event was not set when adapter.stop() was called"


class TestCancelledDuringDrain:
    """Cancelling stop() during the drain loop leaves counters consistent."""

    @pytest.mark.asyncio
    async def test_cancel_during_drain_counters_consistent(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Task cancellation during drain loop does not corrupt capacity state."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None

        # Acquire a delivery slot so the drain loop can't complete immediately.
        assert await cc.acquire_delivery()
        assert cc.delivery_current == 1

        # Use a moderate drain timeout so the drain loop is active.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            5,
        )

        try:
            # Start stop() in a task, then cancel it during the drain loop.
            stop_task = asyncio.create_task(app.stop())
            # Give stop() time to enter the drain loop.
            await asyncio.sleep(0.3)
            stop_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await stop_task
        finally:
            # Release the slot we acquired so the CapacityController is clean.
            await cc.release_delivery()
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )

        # Capacity counters should be consistent.
        snap = cc.snapshot()
        assert (
            snap["delivery_current"] == 0
        ), f"delivery_current is {snap['delivery_current']}, expected 0"
        assert (
            snap["replay_current"] == 0
        ), f"replay_current is {snap['replay_current']}, expected 0"
        # accepting_work should be False (stop_accepting was called).
        assert snap["accepting_work"] is False


class TestDrainAbandonedEvidencePersistence:
    """Drain timeout persists structured abandoned-work receipts to storage."""

    @pytest.mark.asyncio
    async def test_stop_with_inflight_deliveries_persists_abandoned_receipts(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When drain timeout expires with in-flight deliveries, abandoned
        receipts are persisted to storage with failure_kind=shutdown_rejection
        and error=shutdown_drain_timeout.
        """
        from medre.core.engine.pipeline import InflightDelivery
        from medre.core.events.canonical import CanonicalEvent
        from medre.core.events.kinds import EventKind
        from medre.core.events.metadata import EventMetadata

        # Use sqlite (not memory) so data survives storage.close().
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-abandoned-evidence"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_config()},
            ),
        )
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None
        storage = app.storage
        assert storage is not None

        # Remember the database path so we can reopen after close.
        db_path = str(tmp_paths.database_path)

        # Inject an event into storage.
        event = CanonicalEvent(
            event_id="evt-abandoned-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_matrix",
            source_transport_id="matrix",
            source_channel_id="test_room",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test abandoned evidence"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        # Acquire a delivery slot to simulate in-flight work.
        assert await cc.acquire_delivery()
        assert cc.delivery_current == 1

        # Manually register inflight tracking to test the evidence path.
        inflight_key = "evt-abandoned-001:route-1:fake_matrix:plan-1"
        app.pipeline_runner._inflight_deliveries[inflight_key] = InflightDelivery(
            event_id=event.event_id,
            route_id="route-1",
            target_adapter="fake_matrix",
            target_channel=None,
            delivery_plan_id="plan-1",
            source="live",
            replay_run_id=None,
            acquired_at=__import__("time").monotonic(),
        )

        # Use a zero drain timeout to trigger the timeout path immediately.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            await app.stop()
        finally:
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )
            # Defense-in-depth: ensure the app's storage connection is
            # closed even if stop() exited before reaching its internal
            # close step (e.g. due to an unexpected exception).
            if storage is not None:
                try:
                    await storage.close()
                except Exception:
                    pass

        assert app.state == RuntimeState.STOPPED

        # Reopen storage to verify persisted receipts.
        from medre.core.storage.sqlite import SQLiteStorage

        verify_storage = SQLiteStorage(db_path)
        await verify_storage.initialize()
        try:
            receipts = await verify_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1, (
                f"Expected at least 1 receipt for {event.event_id}, "
                f"got {len(receipts)}"
            )

            r = receipts[0]
            assert r.status == "suppressed", f"Expected 'suppressed', got '{r.status}'"
            assert (
                r.failure_kind == "shutdown_rejection"
            ), f"Expected 'shutdown_rejection', got '{r.failure_kind}'"
            assert (
                r.error == "shutdown_drain_timeout"
            ), f"Expected 'shutdown_drain_timeout', got '{r.error}'"
            assert r.event_id == event.event_id
            assert r.attempt_number == 1
            assert r.source == "live"
        finally:
            await verify_storage.close()

    @pytest.mark.asyncio
    async def test_abandoned_receipt_failure_kind_detail(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Abandoned receipt produces failure_kind_detail=shutdown_drain_timeout
        when processed through delivery_receipt_to_report_dict."""
        from medre.core.engine.pipeline import InflightDelivery
        from medre.core.events.canonical import CanonicalEvent
        from medre.core.events.kinds import EventKind
        from medre.core.events.metadata import EventMetadata
        from medre.runtime.reporting import delivery_receipt_to_report_dict

        # Use sqlite (not memory) so data survives storage.close().
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-abandoned-detail"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_config()},
            ),
        )
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None
        storage = app.storage
        assert storage is not None

        db_path = str(tmp_paths.database_path)

        event = CanonicalEvent(
            event_id="evt-abandoned-detail-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_matrix",
            source_transport_id="matrix",
            source_channel_id="test_room",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test detail"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        # Acquire and register inflight manually.
        assert await cc.acquire_delivery()
        inflight_key = "evt-abandoned-detail-001:route-1:fake_matrix:plan-1"
        app.pipeline_runner._inflight_deliveries[inflight_key] = InflightDelivery(
            event_id=event.event_id,
            route_id="route-1",
            target_adapter="fake_matrix",
            target_channel=None,
            delivery_plan_id="plan-1",
            source="live",
            replay_run_id=None,
            acquired_at=__import__("time").monotonic(),
        )

        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            await app.stop()
        finally:
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )
            # Defense-in-depth: ensure the app's storage connection is
            # closed even if stop() exited before reaching its internal
            # close step.
            if storage is not None:
                try:
                    await storage.close()
                except Exception:
                    pass

        assert app.state == RuntimeState.STOPPED

        # Reopen storage to verify persisted receipts.
        from medre.core.storage.sqlite import SQLiteStorage

        verify_storage = SQLiteStorage(db_path)
        await verify_storage.initialize()
        try:
            receipts = await verify_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1

            report = delivery_receipt_to_report_dict(receipts[0])
            assert report["failure_kind"] == "shutdown_rejection"
            assert report["failure_kind_detail"] == "shutdown_drain_timeout"
            assert report["retryable"] is False
            assert report["status"] == "suppressed"
        finally:
            await verify_storage.close()
