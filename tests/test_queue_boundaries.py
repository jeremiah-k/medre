"""Track 6+7: Queue boundary and invariant tests.

Verifies that:
1. All delivery paths acquire capacity before proceeding.
2. Replay cannot bypass capacity queues.
3. Shutdown cancels replay cleanly without hanging.
4. Queues / capacity stay bounded under load.
5. No transport SDK imports leak into runtime modules.
6. Shutdown drain is observable via diagnostics snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.diagnostics.replay_metrics import ReplayMetrics
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.kinds import EventKind
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.runtime.capacity import CapacityController
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.source_reader import source_of as _source_of

# ---------------------------------------------------------------------------
# Shared constants & helpers
# ---------------------------------------------------------------------------

# Queue-specific SDK packages — uses dist/package names (mindroom, reticulum)
# rather than PyPI names (nio, RNS).  Intentionally NOT the canonical _SDK_PACKAGES
# from architecture_report — those use PyPI import names.
_QUEUE_SDK_PACKAGES = (
    "mindroom",
    "meshtastic",
    "meshcore",
    "lxmf",
    "reticulum",
    "nio",
    "RNS",
    "LXMF",
)
"""Third-party transport SDK package names as they appear in import statements."""

# Config-level references (e.g. MeshtasticConfig, MeshtasticRuntimeConfig)
# that are *not* SDK imports.  Only check import lines for these.
_SDK_IMPORT_ONLY_MODULES = frozenset(
    {
        "medre.runtime.builder",  # imports MeshtasticConfig etc, not SDKs
    }
)

_RUNTIME_MODULES = (
    "medre.core.runtime.capacity",
    "medre.runtime.app",
    "medre.runtime.builder",
    "medre.runtime.observability",
)


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
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


def _make_event(
    event_id: str = "evt-boundary-001",
    source_adapter: str = "fake_src",
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "boundary test"},
        metadata=EventMetadata(),
    )


def _make_config_with_fake_matrix(
    adapter_id: str = "mx_boundary",
    limits: RuntimeLimits | None = None,
) -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-boundary"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        limits=limits
        or RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            shutdown_drain_timeout_seconds=2,
            delivery_acquire_timeout_seconds=0.5,
        ),
        adapters=AdapterConfigSet(
            matrix={
                adapter_id: MatrixRuntimeConfig(
                    adapter_id=adapter_id,
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
    )


def _import_lines(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


# ===================================================================
# Test 1: All delivery paths acquire capacity
# ===================================================================


class TestAllDeliveryPathsAcquireCapacity:
    """Verify that delivery through PipelineRunner acquires/releases capacity."""

    async def test_delivery_increments_then_decrements_capacity(self) -> None:
        """A single delivery should acquire then release a capacity slot."""
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=1.0,
        )
        cc = CapacityController(limits)

        snap_before = cc.snapshot()
        assert snap_before["delivery_current"] == 0

        acquired = await cc.acquire_delivery()
        assert acquired is True
        snap_during = cc.snapshot()
        assert snap_during["delivery_current"] == 1

        await cc.release_delivery()
        snap_after = cc.snapshot()
        assert snap_after["delivery_current"] == 0

    async def test_delivery_via_pipeline_runner_uses_capacity(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """PipelineRunner.deliver_to_targets acquires capacity when wired."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        cc = app._capacity_controller
        assert cc is not None

        await app.start()
        try:
            snap_before = cc.snapshot()
            assert snap_before["delivery_current"] == 0

            # Build a minimal route target delivery.
            event = _make_event()
            route = Route(
                id="r-boundary",
                source=RouteSource(
                    adapter="mx_boundary", event_kinds=(), channel="ch-0"
                ),
                targets=[RouteTarget(adapter="mx_boundary", channel="ch-out")],
            )
            plan = DeliveryPlan(
                plan_id="p-1",
                event_id=event.event_id,
                target=RouteTarget(adapter="mx_boundary", channel="ch-out"),
                primary_strategy=DeliveryStrategy(method="direct"),
            )
            # PipelineRunner.deliver_to_targets needs a started runner.
            outcomes = await app.pipeline_runner.deliver_to_targets(
                event,
                [(route, plan)],
            )
            assert len(outcomes) == 1
            # After delivery completes, capacity must be released.
            snap_after = cc.snapshot()
            assert snap_after["delivery_current"] == 0
        finally:
            await app.stop()

    async def test_capacity_rejection_returns_failure_outcome(self) -> None:
        """When capacity is exhausted, deliver returns capacity-exceeded outcomes."""
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            max_inflight_replay_events=1,
            delivery_acquire_timeout_seconds=0.01,
        )
        cc = CapacityController(limits)

        # Exhaust the single slot.
        acquired = await cc.acquire_delivery()
        assert acquired is True

        # Second acquire should time out.
        acquired2 = await cc.acquire_delivery()
        assert acquired2 is False

        snap = cc.snapshot()
        assert snap["delivery_timeouts"] >= 1

        # Clean up.
        await cc.release_delivery()


# ===================================================================
# Test 2: Replay cannot bypass queues
# ===================================================================


class TestReplayCannotBypassQueues:
    """Verify replay delivery requires capacity acquisition."""

    async def test_acquire_replay_increments_counter(self) -> None:
        """acquire_replay increments replay_current."""
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=0.5,
        )
        cc = CapacityController(limits)

        assert cc.snapshot()["replay_current"] == 0
        acquired = await cc.acquire_replay()
        assert acquired is True
        assert cc.snapshot()["replay_current"] == 1

        await cc.release_replay()
        assert cc.snapshot()["replay_current"] == 0

    async def test_replay_rejected_when_capacity_exhausted(self) -> None:
        """acquire_replay returns False when slots are full."""
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            max_inflight_replay_events=1,
            delivery_acquire_timeout_seconds=0.01,
        )
        cc = CapacityController(limits)

        # Exhaust the single replay slot.
        acquired = await cc.acquire_replay()
        assert acquired is True

        # Second acquire should time out.
        acquired2 = await cc.acquire_replay()
        assert acquired2 is False

        snap = cc.snapshot()
        assert snap["replay_timeouts"] >= 1

        await cc.release_replay()

    async def test_replay_rejected_when_not_accepting_work(self) -> None:
        """acquire_replay returns False immediately after stop_accepting."""
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=1.0,
        )
        cc = CapacityController(limits)

        cc.stop_accepting()
        acquired = await cc.acquire_replay()
        assert acquired is False

        snap = cc.snapshot()
        assert snap["replay_rejections"] >= 1
        assert snap["accepting_work"] is False

    async def test_delivery_rejected_when_not_accepting_work(self) -> None:
        """acquire_delivery returns False immediately after stop_accepting."""
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=1.0,
        )
        cc = CapacityController(limits)

        cc.stop_accepting()
        acquired = await cc.acquire_delivery()
        assert acquired is False

        snap = cc.snapshot()
        assert snap["delivery_rejections"] >= 1


# ===================================================================
# Test 3: Shutdown cancels replay cleanly
# ===================================================================


class TestShutdownCancelsReplayCleanly:
    """Verify that runtime shutdown completes without hanging during replay."""

    async def test_shutdown_completes_within_timeout(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Runtime stop() completes within shutdown_drain_timeout_seconds."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        assert app.state is RuntimeState.RUNNING

        # Stop should complete within a reasonable timeout (drain timeout + margin).
        drain_timeout = config.limits.shutdown_drain_timeout_seconds
        margin = 5.0
        await asyncio.wait_for(app.stop(), timeout=drain_timeout + margin)
        assert app.state is RuntimeState.STOPPED

    async def test_shutdown_sets_not_accepting_work(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stop(), capacity controller no longer accepts work."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        cc = app._capacity_controller
        assert cc is not None
        assert cc.accepting_work is True

        await app.stop()
        assert cc.accepting_work is False

    async def test_shutdown_idempotent(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Calling stop() twice does not raise."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        await app.stop()
        assert app.state is RuntimeState.STOPPED

        # Second stop is a no-op.
        await app.stop()
        assert app.state is RuntimeState.STOPPED

    async def test_stop_without_start_is_safe(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Calling stop() on an uninitialized app is a no-op."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        assert app.state is RuntimeState.INITIALIZED
        await app.stop()
        assert app.state is RuntimeState.INITIALIZED


# ===================================================================
# Test 4: Queues stay bounded
# ===================================================================


class TestQueuesStayBounded:
    """Verify that capacity semaphores enforce limits."""

    async def test_delivery_never_exceeds_limit(self) -> None:
        """Concurrent deliveries cannot exceed delivery_limit."""
        limit = 3
        limits = RuntimeLimits(
            max_inflight_deliveries=limit,
            max_inflight_replay_events=10,
            delivery_acquire_timeout_seconds=0.05,
        )
        cc = CapacityController(limits)

        acquired_count = 0
        acquired_slots: list[bool] = []

        # Acquire all slots.
        for _ in range(limit):
            ok = await cc.acquire_delivery()
            acquired_slots.append(ok)
            if ok:
                acquired_count += 1

        assert acquired_count == limit
        assert cc.delivery_current == limit

        # Next acquire should fail (timeout).
        extra = await cc.acquire_delivery()
        assert extra is False
        assert cc.delivery_current == limit

        # Release all.
        for _ in range(acquired_count):
            await cc.release_delivery()
        assert cc.delivery_current == 0

    async def test_replay_never_exceeds_limit(self) -> None:
        """Concurrent replay operations cannot exceed replay_limit."""
        limit = 2
        limits = RuntimeLimits(
            max_inflight_deliveries=10,
            max_inflight_replay_events=limit,
            delivery_acquire_timeout_seconds=0.05,
        )
        cc = CapacityController(limits)

        # Acquire all replay slots.
        for _ in range(limit):
            ok = await cc.acquire_replay()
            assert ok is True

        assert cc.replay_current == limit

        # Next acquire should time out.
        extra = await cc.acquire_replay()
        assert extra is False

        # Release all.
        for _ in range(limit):
            await cc.release_replay()
        assert cc.replay_current == 0

    async def test_snapshot_never_shows_exceeded_limit(self) -> None:
        """Under concurrent pressure, snapshot always shows current <= limit."""
        limit = 5
        limits = RuntimeLimits(
            max_inflight_deliveries=limit,
            max_inflight_replay_events=limit,
            delivery_acquire_timeout_seconds=0.01,
        )
        cc = CapacityController(limits)

        async def _worker() -> None:
            for _ in range(20):
                ok = await cc.acquire_delivery()
                if ok:
                    snap = cc.snapshot()
                    assert (
                        snap["delivery_current"] <= limit
                    ), f"delivery_current {snap['delivery_current']} > limit {limit}"
                    await cc.release_delivery()

        # Run many concurrent workers.
        await asyncio.gather(*[_worker() for _ in range(10)])

        snap = cc.snapshot()
        assert snap["delivery_current"] == 0

    async def test_capacity_controller_in_runtime_enforces_bounds(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Runtime CapacityController limits are respected during lifecycle."""
        config = _make_config_with_fake_matrix(
            limits=RuntimeLimits(
                max_inflight_deliveries=1,
                max_inflight_replay_events=1,
                delivery_acquire_timeout_seconds=0.1,
                shutdown_drain_timeout_seconds=1,
            )
        )
        app = RuntimeBuilder(config, tmp_paths).build()
        cc = app._capacity_controller
        assert cc is not None

        assert cc.delivery_limit == 1
        assert cc.replay_limit == 1

        await app.start()
        await app.stop()


# ===================================================================
# Test 5: No transport SDK imports in runtime modules
# ===================================================================


class TestNoTransportSDKImports:
    """Runtime modules must not import transport SDK packages."""

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_runtime_module_has_no_sdk_imports(self, module_name: str) -> None:
        """Each runtime module must not import any transport SDK."""
        try:
            source = _source_of(module_name)
        except (ImportError, ModuleNotFoundError):
            pytest.skip(f"Module {module_name} not importable")

        lines = _import_lines(source)
        for sdk in _QUEUE_SDK_PACKAGES:
            for line in lines:
                assert not re.search(
                    rf"\b{re.escape(sdk)}\b", line
                ), f"{module_name} imports transport SDK '{sdk}': {line}"

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_runtime_module_source_has_no_sdk_references(
        self,
        module_name: str,
    ) -> None:
        """Full source text of runtime modules must not reference SDKs.

        Exception: builder.py references adapter config names like
        MeshtasticConfig which contain SDK names but are not actual SDK
        imports — those are checked via import-line analysis only.
        """
        try:
            source = _source_of(module_name)
        except (ImportError, ModuleNotFoundError):
            pytest.skip(f"Module {module_name} not importable")

        # For modules that legitimately reference adapter config names,
        # only check import lines (not full source text).
        if module_name in _SDK_IMPORT_ONLY_MODULES:
            lines = _import_lines(source)
            for sdk in _QUEUE_SDK_PACKAGES:
                for line in lines:
                    assert not re.search(
                        rf"\b{re.escape(sdk)}\b", line
                    ), f"{module_name} imports SDK '{sdk}': {line}"
            return

        for sdk in _QUEUE_SDK_PACKAGES:
            assert (
                sdk not in source
            ), f"{module_name} references SDK '{sdk}' in source text"


# ===================================================================
# Test 6: Shutdown drain observable
# ===================================================================


class TestShutdownDrainObservable:
    """Verify that shutdown drain outcomes are observable via diagnostics."""

    async def test_diagnostic_snapshot_includes_capacity(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """MedreApp.diagnostic_snapshot() includes capacity controller data."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        try:
            snap = app.diagnostic_snapshot()
            assert "capacity" in snap
            assert snap["capacity"] is not None
            assert "delivery_current" in snap["capacity"]
            assert "delivery_limit" in snap["capacity"]
            assert "replay_current" in snap["capacity"]
            assert "replay_limit" in snap["capacity"]
            assert "accepting_work" in snap["capacity"]
            assert "delivery_rejections" in snap["capacity"]
            assert "delivery_timeouts" in snap["capacity"]
            assert "replay_rejections" in snap["capacity"]
            assert "replay_timeouts" in snap["capacity"]
        finally:
            await app.stop()

    async def test_diagnostic_snapshot_shows_accepting_work(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Before stop: accepting_work=True; after stop: False."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        snap_running = app.diagnostic_snapshot()
        assert snap_running["accepting_work"] is True
        assert snap_running["runtime_state"] == "running"

        await app.stop()
        snap_stopped = app.diagnostic_snapshot()
        assert snap_stopped["accepting_work"] is False
        assert snap_stopped["runtime_state"] == "stopped"

    async def test_drain_timeout_in_snapshot(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """diagnostic_snapshot includes shutdown_drain_timeout_seconds."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        await app.start()
        try:
            snap = app.diagnostic_snapshot()
            assert "shutdown_drain_timeout_seconds" in snap
            assert (
                snap["shutdown_drain_timeout_seconds"]
                == config.limits.shutdown_drain_timeout_seconds
            )
        finally:
            await app.stop()

    async def test_state_transitions_are_logged(
        self,
        tmp_paths: MedrePaths,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Runtime state transitions appear in logs during start/stop."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        with caplog.at_level(logging.DEBUG, logger="medre.runtime.app"):
            await app.start()
            await app.stop()

        log_text = caplog.text
        # Verify key lifecycle transitions are logged.
        assert "starting" in log_text.lower() or "STARTING" in log_text
        assert "stopped" in log_text.lower() or "STOPPED" in log_text

    async def test_drain_logs_inflight_counts(
        self,
        tmp_paths: MedrePaths,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When drain completes, logs should reflect the drain outcome."""
        config = _make_config_with_fake_matrix()
        app = RuntimeBuilder(config, tmp_paths).build()

        with caplog.at_level(logging.DEBUG, logger="medre.runtime.app"):
            await app.start()
            await app.stop()

        log_text = caplog.text.lower()
        # Either "drained" (success) or "timed out" (drain timeout) should appear.
        assert "drained" in log_text or "timed out" in log_text or "drain" in log_text


# ===================================================================
# Track 6+7: ReplayMetrics extended diagnostics
# ===================================================================


class TestReplayMetricsDiagnostics:
    """Verify ReplayMetrics exposes backlog, rejection, cancellation counters."""

    def test_backlog_estimate_defaults_to_zero(self) -> None:
        rm = ReplayMetrics()
        snap = rm.snapshot()
        assert snap["global"]["backlog_estimate"] == 0

    def test_set_backlog_estimate_updates_snapshot(self) -> None:
        rm = ReplayMetrics()
        rm.set_backlog_estimate(42)
        snap = rm.snapshot()
        assert snap["global"]["backlog_estimate"] == 42

    def test_set_backlog_estimate_clamps_to_zero(self) -> None:
        rm = ReplayMetrics()
        rm.set_backlog_estimate(-5)
        snap = rm.snapshot()
        assert snap["global"]["backlog_estimate"] == 0

    def test_rejection_count_defaults_to_zero(self) -> None:
        rm = ReplayMetrics()
        snap = rm.snapshot()
        assert snap["global"]["rejection_count"] == 0

    def test_record_rejection_increments(self) -> None:
        rm = ReplayMetrics()
        rm.record_rejection()
        rm.record_rejection()
        snap = rm.snapshot()
        assert snap["global"]["rejection_count"] == 2

    def test_last_cancelled_at_defaults_to_none(self) -> None:
        rm = ReplayMetrics()
        snap = rm.snapshot()
        assert snap["global"]["last_cancelled_at"] is None

    def test_record_cancellation_sets_timestamp(self) -> None:
        rm = ReplayMetrics()
        before = _time.monotonic()
        rm.record_cancellation()
        after = _time.monotonic()

        snap = rm.snapshot()
        ts = snap["global"]["last_cancelled_at"]
        assert ts is not None
        assert before <= ts <= after


# ===================================================================
# DiagnosticsCollector integration
# ===================================================================


class TestDiagnosticsCollectorIntegration:
    """Verify DiagnosticsCollector passes capacity snapshot through."""

    def test_snapshot_includes_capacity_when_set(self) -> None:
        from medre.runtime.observability import DiagnosticsCollector

        dc = DiagnosticsCollector()
        capacity_snap = {
            "accepting_work": True,
            "delivery_current": 0,
            "delivery_limit": 10,
        }
        dc.set_capacity_snapshot(capacity_snap)
        snap = dc.snapshot()
        assert "capacity" in snap
        assert snap["capacity"]["delivery_limit"] == 10

    def test_snapshot_omits_capacity_when_not_set(self) -> None:
        from medre.runtime.observability import DiagnosticsCollector

        dc = DiagnosticsCollector()
        snap = dc.snapshot()
        assert "capacity" not in snap

    def test_replay_backlog_observable_via_collector(self) -> None:
        from medre.runtime.observability import DiagnosticsCollector

        dc = DiagnosticsCollector()
        dc.set_replay_backlog_estimate(15)
        dc.record_replay_rejection()
        dc.record_replay_cancellation()

        snap = dc.snapshot()
        assert snap["replay"]["global"]["backlog_estimate"] == 15
        assert snap["replay"]["global"]["rejection_count"] == 1
        assert snap["replay"]["global"]["last_cancelled_at"] is not None


# ===================================================================
# Capacity snapshot determinism
# ===================================================================


class TestCapacitySnapshotDeterminism:
    """Snapshot output must be deterministic and JSON-safe."""

    def test_snapshot_keys_are_sorted(self) -> None:
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        keys = list(snap.keys())
        assert keys == sorted(keys), f"Snapshot keys not sorted: {keys}"

    def test_snapshot_is_json_serializable(self) -> None:
        import json

        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        serialized = json.dumps(snap, sort_keys=True)
        assert isinstance(serialized, str)

    def test_two_snapshots_identical_when_no_changes(self) -> None:
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        s1 = cc.snapshot()
        s2 = cc.snapshot()
        assert s1 == s2
