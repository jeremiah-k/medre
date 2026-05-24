"""Runtime crash/recovery and persistence tests (Wave 3E).

Covers:
- Classification correctness: calling classify_runtime_health() with FAILED
  states after startup correctly identifies DEGRADED/FAILED health. These tests
  supply adapter states to the classification function; they do not exercise
  active post-start failure detection or runtime state transitions.
- Partial adapter startup results in degraded runtime allowed.
- Zero adapters started causes startup failure with clear operator-facing error.
- Replay availability after restart when storage supports it.
- RouteStats reset on restart / new runtime instance.
- CapacityController state reset on restart / new runtime instance.
- SQLite storage persistence survives restart.
- RuntimeAccounting / process-local counters reset on restart / new instance.
- Startup summaries are deterministic in ordering/shape.

Uses fake adapters only, memory/sqlite temp storage only, no live dependencies.
"""

from __future__ import annotations

import json
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
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.lifecycle.states import AdapterState
from medre.core.routing.stats import RouteStats
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.core.supervision.supervision import (
    RuntimeHealth,
    classify_runtime_health,
    runtime_supervision_snapshot,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.boot_summary import BootSummary, build_boot_summary
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FailingAdapter(AdapterContract):
    """Adapter that raises on start() for partial-startup testing."""

    adapter_id: str = "failing_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_adapter") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated adapter failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


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


def _fake_matrix_runtime_config(
    adapter_id: str = "fake_matrix",
    enabled: bool = True,
) -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _fake_meshtastic_runtime_config(
    adapter_id: str = "fake_mesh",
    enabled: bool = True,
) -> MeshtasticRuntimeConfig:
    return MeshtasticRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        adapter_kind="fake",
        config=None,
    )


def _config_with_fake_adapters(
    *,
    storage_backend: str = "memory",
    storage_path: str | None = None,
) -> RuntimeConfig:
    """RuntimeConfig with two fake adapters (matrix + meshtastic)."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend, path=storage_path),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
            meshtastic={"radio": _fake_meshtastic_runtime_config()},
        ),
    )


def _config_with_one_fake_adapter(
    *,
    storage_backend: str = "memory",
) -> RuntimeConfig:
    """RuntimeConfig with one fake adapter."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery-single"),
        storage=StorageConfig(backend=storage_backend),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_runtime_config()},
        ),
    )


def _config_with_no_adapters() -> RuntimeConfig:
    """RuntimeConfig with zero adapters."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-recovery-empty"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


def _make_minimal_event(event_id: str = "evt-001") -> CanonicalEvent:
    """Create a minimal CanonicalEvent for storage tests."""
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
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ===================================================================
# 1. Abrupt adapter failure after startup
# ===================================================================


class TestAbruptAdapterFailureAfterStartup:
    """Classification correctness after startup with supplied adapter states.

    These tests start the runtime, then call classify_runtime_health() with
    explicitly supplied adapter states to verify classification produces
    correct results. The runtime is not mutated — no adapter is actually
    failed at runtime. The tests confirm that (a) the runtime process stays
    RUNNING when classification functions are called with degraded/failed
    states, and (b) classify_runtime_health() returns the expected value
    for those supplied states.
    """

    @pytest.mark.asyncio
    async def test_one_adapter_failure_does_not_kill_runtime(
        self, tmp_paths: MedrePaths
    ) -> None:
        """classify_runtime_health() with [READY, FAILED] states returns DEGRADED after startup."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()
        try:
            assert app.state == RuntimeState.RUNNING

            # Both adapters started.
            assert len(app.started_adapter_ids) == 2
            boot = app.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "success"
            assert boot.runtime_health == "healthy"

            # Classify with one READY + one FAILED (supplied states,
            # not an actual adapter failure in the running runtime).
            post_failure_states = [
                AdapterState.READY,
                AdapterState.FAILED,
            ]
            health = classify_runtime_health(post_failure_states)
            assert health == RuntimeHealth.DEGRADED

            # Runtime itself stays RUNNING — calling classification with
            # failed states does not crash or stop the runtime.
            assert app.state == RuntimeState.RUNNING

            # Supervision snapshot reflects degraded state for supplied states.
            snap = runtime_supervision_snapshot(post_failure_states)
            assert snap["runtime_health"] == "degraded"
            assert snap["adapter_summary"]["healthy"] == 1
            assert snap["adapter_summary"]["failed"] == 1
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_abrupt_failure_produces_degraded_diagnostics(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Classification and diagnostic snapshot remain accessible after startup.

        Calls classify_runtime_health([FAILED]) to verify classification
        returns FAILED, then calls app.diagnostic_snapshot() to confirm the
        runtime remains RUNNING and the snapshot is accessible.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()
        try:
            # Initially healthy.
            assert app.boot_summary is not None
            assert app.boot_summary.runtime_health == "healthy"

            # Classify with sole adapter FAILED (supplied state).
            states = [AdapterState.FAILED]
            health = classify_runtime_health(states)
            assert health == RuntimeHealth.FAILED

            # Diagnostic snapshot is still accessible (runtime stays running
            # even though classification of supplied states returns FAILED).
            snap = app.diagnostic_snapshot()
            assert isinstance(snap, dict)
            assert "runtime_state" in snap
            assert snap["runtime_state"] == "running"
        finally:
            await app.stop()


# ===================================================================
# 2. Partial adapter startup
# ===================================================================


class TestPartialAdapterStartup:
    """Some adapters failing during startup produces degraded running."""

    @pytest.mark.asyncio
    async def test_partial_startup_allows_degraded_running(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One adapter fails on start → runtime enters RUNNING in degraded mode."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Inject a failing adapter by replacing one of the built adapters.
        failing = _FailingAdapter(adapter_id="fake_mesh")
        app.adapters["fake_mesh"] = failing

        await app.start()
        try:
            # Runtime should be RUNNING despite partial startup.
            assert app.state == RuntimeState.RUNNING

            boot = app.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "partial"
            assert boot.runtime_health == "degraded"
            assert boot.adapters_started == 1
            assert boot.adapters_failed == 1
            assert boot.adapters_total == 2
            assert "fake_mesh" in boot.failed_adapter_ids
            assert "fake_matrix" in boot.started_adapter_ids
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_partial_startup_health_state(self, tmp_paths: MedrePaths) -> None:
        """Health state stored on app after partial startup is degraded."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        failing = _FailingAdapter(adapter_id="fake_mesh")
        app.adapters["fake_mesh"] = failing

        await app.start()
        try:
            assert app._health_state is not None
            assert app._health_state["runtime_health"] == "degraded"
        finally:
            await app.stop()


# ===================================================================
# 3. Zero adapters started → total failure
# ===================================================================


class TestZeroAdaptersStartup:
    """Zero adapters started produces RuntimeStartupError."""

    @pytest.mark.asyncio
    async def test_empty_config_raises_startup_error(
        self, tmp_paths: MedrePaths
    ) -> None:
        """No adapters at all raises RuntimeStartupError."""
        config = _config_with_no_adapters()
        app = _build_app(config, tmp_paths)

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_all_adapters_fail_on_start_raises(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All adapters failing on start raises RuntimeStartupError."""
        config = _config_with_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Replace both adapters with failing ones.
        app.adapters["fake_matrix"] = _FailingAdapter(adapter_id="fake_matrix")
        app.adapters["fake_mesh"] = _FailingAdapter(adapter_id="fake_mesh")

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_all_disabled_adapters_raises(self, tmp_paths: MedrePaths) -> None:
        """All adapters disabled → zero started → RuntimeStartupError."""
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="all-disabled"),
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "off": _fake_matrix_runtime_config(enabled=False),
                },
            ),
        )
        app = _build_app(config, tmp_paths)

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.state == RuntimeState.FAILED


# ===================================================================
# 4. Replay after restart uses persisted storage
# ===================================================================


class TestReplayAfterRestart:
    """Replay availability after runtime restart with persistent storage."""

    @pytest.mark.asyncio
    async def test_replay_available_after_sqlite_restart(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Second runtime instance sees replay_available=True with sqlite."""
        config = _config_with_one_fake_adapter(storage_backend="sqlite")
        app1 = _build_app(config, tmp_paths)
        await app1.start()
        boot1 = app1.boot_summary
        assert boot1 is not None
        assert boot1.replay_available is True
        await app1.stop()

        # Build second instance on the same storage path.
        app2 = _build_app(config, tmp_paths)
        await app2.start()
        try:
            boot2 = app2.boot_summary
            assert boot2 is not None
            assert boot2.replay_available is True
            assert boot2.storage_backend == "sqlite"
        finally:
            await app2.stop()

    @pytest.mark.asyncio
    async def test_persisted_events_count_survives_restart(
        self, tmp_paths: MedrePaths, tmp_path: Path
    ) -> None:
        """Events stored in first instance are visible in second instance."""
        db_path = str(tmp_path / "persist_test.db")

        # First instance: store an event.
        storage1 = SQLiteStorage(db_path)
        await storage1.initialize()
        event = _make_minimal_event("evt-persist-001")
        await storage1.append(event)
        count1 = await storage1.count_events()
        assert count1 == 1
        await storage1.close()

        # Second instance: verify event survived.
        storage2 = SQLiteStorage(db_path)
        await storage2.initialize()
        count2 = await storage2.count_events()
        assert count2 == 1
        await storage2.close()


# ===================================================================
# 5. RouteStats reset on restart / new instance
# ===================================================================


class TestRouteStatsReset:
    """RouteStats counters reset on new runtime instance."""

    def test_new_instance_has_empty_counters(self) -> None:
        """Fresh RouteStats has no counters."""
        stats = RouteStats()
        snap = stats.snapshot()
        assert snap == {}

    def test_counters_reset_on_new_instance(self) -> None:
        """A new RouteStats instance starts with zero counters."""
        old_stats = RouteStats()
        old_stats.record_delivered("route-1")
        old_stats.record_delivered("route-1")
        old_stats.record_failed("route-2", "error")
        assert len(old_stats.snapshot()) == 2

        new_stats = RouteStats()
        assert new_stats.snapshot() == {}

    @pytest.mark.asyncio
    async def test_runtime_route_stats_fresh_after_rebuild(
        self, tmp_paths: MedrePaths
    ) -> None:
        """RouteStats on rebuilt app starts empty."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        assert app.route_stats is not None
        # Before start, route_stats should be empty.
        assert app.route_stats.snapshot() == {}


# ===================================================================
# 6. CapacityController reset on restart / new instance
# ===================================================================


class TestCapacityControllerReset:
    """CapacityController state resets on new runtime instance."""

    def test_new_instance_has_fresh_counters(self) -> None:
        """Fresh CapacityController has zero counters."""
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        assert snap["delivery_current"] == 0
        assert snap["replay_current"] == 0
        assert snap["delivery_rejections"] == 0
        assert snap["replay_rejections"] == 0
        assert snap["delivery_timeouts"] == 0
        assert snap["replay_timeouts"] == 0
        assert snap["accepting_work"] is True

    def test_old_counters_not_carried_to_new_instance(self) -> None:
        """A new CapacityController does not inherit old counters."""
        limits = RuntimeLimits()
        old_cc = CapacityController(limits)
        # Mutate internal counters directly to simulate usage.
        old_cc._delivery_rejections = 42
        old_cc._replay_rejections = 10
        old_cc._delivery_timeouts = 5
        old_cc.stop_accepting()

        new_cc = CapacityController(limits)
        snap = new_cc.snapshot()
        assert snap["delivery_rejections"] == 0
        assert snap["replay_rejections"] == 0
        assert snap["delivery_timeouts"] == 0
        assert snap["replay_timeouts"] == 0
        assert snap["accepting_work"] is True

    @pytest.mark.asyncio
    async def test_capacity_controller_on_built_app_is_fresh(
        self, tmp_paths: MedrePaths
    ) -> None:
        """CapacityController wired into a built app starts with fresh state."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        cc = app._capacity_controller
        assert cc is not None
        snap = cc.snapshot()
        assert snap["delivery_current"] == 0
        assert snap["accepting_work"] is True


# ===================================================================
# 7. SQLite/storage persistence survives restart
# ===================================================================


class TestStoragePersistenceSurvivesRestart:
    """Storage data survives runtime restart."""

    @pytest.mark.asyncio
    async def test_event_persists_across_storage_instances(
        self, tmp_path: Path
    ) -> None:
        """An event written in one storage session is readable in the next."""
        db_path = str(tmp_path / "restart_test.db")

        # Session 1: write event.
        s1 = SQLiteStorage(db_path)
        await s1.initialize()
        evt = _make_minimal_event("evt-restart-001")
        await s1.append(evt)
        await s1.close()

        # Session 2: read event.
        s2 = SQLiteStorage(db_path)
        await s2.initialize()
        retrieved = await s2.get("evt-restart-001")
        assert retrieved is not None
        assert retrieved.event_id == "evt-restart-001"
        assert retrieved.source_adapter == "fake_matrix"
        count = await s2.count_events()
        assert count == 1
        await s2.close()

    @pytest.mark.asyncio
    async def test_multiple_events_persist_across_restart(self, tmp_path: Path) -> None:
        """Multiple events survive storage restart."""
        db_path = str(tmp_path / "multi_restart.db")

        s1 = SQLiteStorage(db_path)
        await s1.initialize()
        for i in range(5):
            await s1.append(_make_minimal_event(f"evt-multi-{i:03d}"))
        assert await s1.count_events() == 5
        await s1.close()

        s2 = SQLiteStorage(db_path)
        await s2.initialize()
        assert await s2.count_events() == 5
        await s2.close()

    @pytest.mark.asyncio
    async def test_memory_storage_does_not_persist(self, tmp_path: Path) -> None:
        """In-memory storage does NOT persist across instances."""
        s1 = SQLiteStorage(":memory:")
        await s1.initialize()
        await s1.append(_make_minimal_event("evt-mem-001"))
        assert await s1.count_events() == 1
        await s1.close()

        s2 = SQLiteStorage(":memory:")
        await s2.initialize()
        assert await s2.count_events() == 0
        await s2.close()


# ===================================================================
# 8. RuntimeAccounting reset on new instance
# ===================================================================


class TestRuntimeAccountingReset:
    """Process-local counters reset on restart / new instance."""

    def test_fresh_instance_all_zeros(self) -> None:
        """New RuntimeAccounting has all counters at zero."""
        acc = RuntimeAccounting()
        snap = acc.snapshot()
        assert all(v == 0 for v in snap.values())

    def test_old_counters_not_carried_to_new_instance(self) -> None:
        """A new RuntimeAccounting instance starts at zero."""
        old_acc = RuntimeAccounting()
        old_acc.record_inbound_accepted()
        old_acc.record_inbound_accepted()
        old_acc.record_outbound_attempt()
        old_acc.record_outbound_delivered()
        old_acc.record_replay_processed()
        assert old_acc.counters().inbound_accepted == 2

        new_acc = RuntimeAccounting()
        assert new_acc.counters().inbound_accepted == 0
        assert new_acc.counters().outbound_attempts == 0
        assert new_acc.counters().outbound_delivered == 0
        assert new_acc.counters().replay_processed == 0
        snap = new_acc.snapshot()
        assert all(v == 0 for v in snap.values())

    def test_explicit_reset_returns_previous_and_zeros(self) -> None:
        """reset() returns previous counters and zeros everything."""
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_failed()

        prev = acc.reset()
        assert prev.inbound_accepted == 1
        assert prev.outbound_failed == 1

        # After reset, all zeros.
        assert acc.counters().inbound_accepted == 0
        assert acc.counters().outbound_failed == 0

    @pytest.mark.asyncio
    async def test_accounting_on_built_app_is_fresh(
        self, tmp_paths: MedrePaths
    ) -> None:
        """RuntimeAccounting wired into a built app starts at zero."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        acc = app._runtime_accounting
        assert acc is not None
        snap = acc.snapshot()
        assert all(v == 0 for v in snap.values())


# ===================================================================
# 9. Deterministic startup summaries
# ===================================================================


class TestDeterministicStartupSummaries:
    """Startup summaries have deterministic ordering and shape."""

    def test_boot_summary_keys_alphabetically_sorted(self) -> None:
        """to_dict() produces alphabetically sorted keys."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=2,
            adapters_failed=0,
            adapters_total=2,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["beta_adapter", "alpha_adapter"],
            route_count=3,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=0,
        )
        d = bs.to_dict()
        keys = list(d.keys())
        assert keys == sorted(keys)

    def test_adapter_ids_sorted_in_summary(self) -> None:
        """Adapter ID lists are sorted regardless of input order."""
        bs = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=2,
            adapters_failed=1,
            adapters_total=3,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=["gamma", "alpha", "beta"],
            started_adapter_ids=["z_adapter", "a_adapter"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        assert bs.failed_adapter_ids == ("alpha", "beta", "gamma")
        assert bs.started_adapter_ids == ("a_adapter", "z_adapter")

    def test_boot_summary_json_serializable(self) -> None:
        """Boot summary to_dict() is JSON-serializable."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["adapter-1"],
            route_count=5,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=42,
        )
        serialized = json.dumps(bs.to_dict())
        assert isinstance(serialized, str)
        deserialized = json.loads(serialized)
        assert deserialized["adapters_started"] == 1

    def test_boot_summary_shape_deterministic_across_calls(self) -> None:
        """Multiple summaries with same inputs produce identical shapes."""

        def _make_summary() -> BootSummary:
            return build_boot_summary(
                startup_timestamp="2026-05-11T12:00:00+00:00",
                startup_outcome="partial",
                runtime_health="degraded",
                adapters_started=2,
                adapters_failed=1,
                adapters_total=3,
                adapters_disabled=1,
                build_failure_count=0,
                failed_adapter_ids=["c_adapter"],
                started_adapter_ids=["a_adapter", "b_adapter"],
                route_count=2,
                storage_backend="sqlite",
                replay_available=True,
                persisted_events_count=10,
            )

        summaries = [_make_summary() for _ in range(10)]
        dicts = [s.to_dict() for s in summaries]

        # All dicts have identical keys in identical order.
        key_lists = [list(d.keys()) for d in dicts]
        assert all(kl == key_lists[0] for kl in key_lists)

        # All values identical.
        for d in dicts[1:]:
            assert d == dicts[0]

    def test_boot_summary_timestamp_is_string_or_none(self) -> None:
        """startup_timestamp is either a string or None, not a datetime object."""
        bs_with_ts = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        assert isinstance(bs_with_ts.startup_timestamp, str)

        bs_no_ts = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        assert bs_no_ts.startup_timestamp is None

    @pytest.mark.asyncio
    async def test_boot_summary_populated_after_start(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Boot summary is non-None and valid after app.start()."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()
        try:
            boot = app.boot_summary
            assert boot is not None
            assert boot.startup_outcome == "success"
            assert boot.runtime_health == "healthy"
            assert boot.adapters_started == 1
            assert boot.adapters_total == 1

            # to_dict() is JSON-safe.
            d = boot.to_dict()
            json.dumps(d)

            # Keys sorted.
            keys = list(d.keys())
            assert keys == sorted(keys)
        finally:
            await app.stop()

    @pytest.mark.asyncio
    async def test_startup_outcome_deterministic_for_same_config(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Two start/stop cycles produce same startup outcome."""
        config = _config_with_one_fake_adapter()
        outcomes: list[str] = []

        for _ in range(2):
            app = _build_app(config, tmp_paths)
            await app.start()
            assert app.boot_summary is not None
            outcomes.append(app.boot_summary.startup_outcome)
            await app.stop()

        assert outcomes[0] == outcomes[1]


# ===================================================================
# 10. RouteStats snapshot structure after usage
# ===================================================================


class TestRouteStatsSnapshotStructure:
    """RouteStats snapshot structure is deterministic after recording."""

    def test_snapshot_keys_sorted(self) -> None:
        """Snapshot keys are route IDs in sorted order."""
        stats = RouteStats()
        stats.record_delivered("zebra")
        stats.record_delivered("alpha")
        stats.record_delivered("mid")

        snap = stats.snapshot()
        keys = list(snap.keys())
        assert keys == ["alpha", "mid", "zebra"]

    def test_snapshot_json_serializable(self) -> None:
        """Snapshot is JSON-serializable."""
        stats = RouteStats()
        stats.record_delivered("route-1")
        stats.record_failed("route-2", "connection refused at 192.168.1.1")
        stats.record_loop_prevented("route-3")

        snap = stats.snapshot()
        serialized = json.dumps(snap)
        assert isinstance(serialized, str)

    def test_counters_shape_per_route(self) -> None:
        """Each route in snapshot has correct counter shape."""
        stats = RouteStats()
        stats.record_delivered("r1")
        stats.record_failed("r1", "err")

        snap = stats.snapshot()
        r1 = snap["r1"]
        # RouteStats snapshot has flat counter keys per route.
        assert r1["delivered"] == 1
        assert r1["failed"] == 1
        assert r1["skipped"] == 0
        assert r1["loop_prevented"] == 0
        assert "last_error" in r1


# ===================================================================
# 11. CapacityController snapshot structure
# ===================================================================


class TestCapacityControllerSnapshotStructure:
    """CapacityController snapshot has deterministic structure."""

    def test_snapshot_keys_sorted(self) -> None:
        """Snapshot keys are alphabetically sorted."""
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        keys = list(snap.keys())
        assert keys == sorted(keys)

    def test_snapshot_json_serializable(self) -> None:
        """Snapshot is JSON-serializable with no secrets."""
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        serialized = json.dumps(snap)
        assert isinstance(serialized, str)

    def test_snapshot_contains_expected_fields(self) -> None:
        """Snapshot contains all expected capacity fields."""
        limits = RuntimeLimits()
        cc = CapacityController(limits)
        snap = cc.snapshot()
        expected_fields = {
            "accepting_work",
            "delivery_current",
            "delivery_limit",
            "delivery_rejections",
            "delivery_timeouts",
            "replay_current",
            "replay_limit",
            "replay_rejections",
            "replay_timeouts",
        }
        assert set(snap.keys()) == expected_fields


# ===================================================================
# 12. RuntimeAccounting snapshot structure
# ===================================================================


class TestRuntimeAccountingSnapshotStructure:
    """RuntimeAccounting snapshot has deterministic structure."""

    def test_snapshot_keys_sorted(self) -> None:
        """Snapshot keys are alphabetically sorted."""
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        snap = acc.snapshot()
        keys = list(snap.keys())
        assert keys == sorted(keys)

    def test_snapshot_json_serializable(self) -> None:
        """Snapshot is JSON-serializable."""
        acc = RuntimeAccounting()
        for _ in range(3):
            acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        snap = acc.snapshot()
        serialized = json.dumps(snap)
        assert isinstance(serialized, str)

    def test_exactly_eight_counters(self) -> None:
        """Snapshot always has exactly 8 counters."""
        acc = RuntimeAccounting()
        snap = acc.snapshot()
        assert len(snap) == 8
        acc.record_inbound_accepted()
        snap2 = acc.snapshot()
        assert len(snap2) == 8
