"""Startup/shutdown and snapshot integration tests.

Multi-adapter startup with partial and total failure scenarios, shutdown
idempotency, and detailed snapshot field-level assertions — all with fake
adapters and in-memory storage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
    wait_until,
)

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
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ===================================================================
# STARTUP / SHUTDOWN INTEGRATION TESTS
# ===================================================================


class TestStartupShutdownIntegration:
    """Multi-adapter startup, partial failure, total failure, and shutdown coverage."""

    @pytest.mark.asyncio
    async def test_multi_adapter_successful_startup(
        self, tmp_paths: MedrePaths
    ) -> None:
        """All 4 adapters start successfully → HEALTHY."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            assert app.state is RuntimeState.RUNNING
            assert app.boot_summary is not None
            assert app.boot_summary.runtime_health == "healthy"
            assert len(app.started_adapter_ids) == 4
            assert len(app.boot_summary.failed_adapter_ids) == 0
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_partial_startup_degraded_running(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Partial adapter startup → DEGRADED + RUNNING."""
        config = make_multi_adapter_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()
        # Monkey-patch one adapter's start to fail.
        failing = app.adapters["fake_lxmf"]
        original_start = failing.start

        async def _fail_start(ctx: Any) -> None:
            raise RuntimeError("simulated lxmf start failure")

        failing.start = _fail_start  # type: ignore[assignment]
        await app.start()
        try:
            assert app.state is RuntimeState.RUNNING
            assert app.boot_summary is not None
            assert app.boot_summary.adapters_started == 3
            assert app.boot_summary.adapters_failed == 1
            assert "fake_lxmf" in app.boot_summary.failed_adapter_ids
        finally:
            failing.start = original_start  # type: ignore[assignment]
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_total_startup_failure_raises(self, tmp_paths: MedrePaths) -> None:
        """All adapters fail to start → RuntimeStartupError + FAILED state."""
        from medre.runtime.errors import RuntimeStartupError

        config = make_multi_adapter_config()
        builder = RuntimeBuilder(config, tmp_paths)
        app = builder.build()

        async def _fail_start(ctx: Any) -> None:
            raise RuntimeError("total failure simulation")

        for adapter in app.adapters.values():
            adapter.start = _fail_start  # type: ignore[assignment]

        with pytest.raises(RuntimeStartupError):
            await app.start()
        assert app.state is RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_clean_shutdown_transitions_adapters_to_stopped(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stop, all started adapters transition to STOPPED."""
        from medre.core.lifecycle.states import AdapterState

        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            await clean_stop(app)
            for aid, state in app.adapter_states.items():
                assert (
                    state is AdapterState.STOPPED
                ), f"Adapter {aid} in state {state}, expected STOPPED"
        except Exception:
            await app.stop()
            raise

    @pytest.mark.asyncio
    async def test_concurrent_stop_idempotent(self, tmp_paths: MedrePaths) -> None:
        """Concurrent stop() calls are idempotent and do not raise."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        # Fire two concurrent stops.
        results = await asyncio.gather(
            app.stop(),
            app.stop(),
            return_exceptions=True,
        )
        # None should be exceptions.
        for r in results:
            assert not isinstance(r, Exception), f"Unexpected exception: {r}"
        assert app.state is RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_shutdown_stops_accepting_delivery_work(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """After stop(), capacity controller no longer accepts work."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        assert app._capacity_controller is not None
        assert app._capacity_controller.accepting_work

        await clean_stop(app)
        assert not app._capacity_controller.accepting_work


# ===================================================================
# SNAPSHOT INTEGRATION TESTS
# ===================================================================


class TestSnapshotIntegration:
    """Detailed snapshot assertions: schema_version, lifecycle, health, routes, accounting, diagnostics."""

    @pytest.mark.asyncio
    async def test_schema_version_is_one(self, tmp_paths: MedrePaths) -> None:
        """Snapshot schema_version is exactly 1."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == 1
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_lifecycle_section(self, tmp_paths: MedrePaths) -> None:
        """Lifecycle section has runtime_state, startup_timestamp, uptime_seconds."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            lc = snap["lifecycle"]
            assert lc["runtime_state"] == "running"
            assert lc["startup_timestamp"] is not None
            assert lc["uptime_seconds"] is not None
            assert lc["uptime_seconds"] >= 0
            assert "adapters" in lc
            assert len(lc["adapters"]) == 4
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_health_section(self, tmp_paths: MedrePaths) -> None:
        """Health section: live_health is null before refresh, startup_health present."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            assert snap["health"]["live_health"] is None
            assert snap["health"]["scope"] == "startup"
            assert snap["health"]["live_refresh"] is False
            assert snap["startup"]["startup_health"] is not None
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_startup_outcome_section(self, tmp_paths: MedrePaths) -> None:
        """Startup section: boot_summary, startup_health, build_failures."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            su = snap["startup"]
            assert su["boot_summary"] is not None
            assert su["boot_summary"]["startup_outcome"] == "success"
            assert su["boot_summary"]["runtime_health"] == "healthy"
            assert su["startup_health"] is not None
            assert su["build_failures"] == []
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_routes_build_readiness_and_startup_readiness(
        self,
        tmp_paths: MedrePaths,
    ) -> None:
        """Routes section has build_readiness, startup_readiness, eligibility."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            routes = snap["routes"]
            assert "build_readiness" in routes
            assert "startup_readiness" in routes
            assert "eligibility" in routes
            assert "stats" in routes
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_accounting_section(self, tmp_paths: MedrePaths) -> None:
        """Accounting section has all 8 counters."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            acc = snap["accounting"]["counters"]
            assert acc is not None
            for key in (
                "inbound_accepted",
                "outbound_attempts",
                "outbound_delivered",
                "outbound_failed",
                "replay_processed",
                "replay_rejected",
                "loop_prevented",
                "capacity_rejections",
            ):
                assert key in acc, f"Missing accounting key: {key}"
                assert isinstance(acc[key], int)
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_diagnostics_json_safe(self, tmp_paths: MedrePaths) -> None:
        """Diagnostics section is JSON-safe."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            # Must be JSON-serialisable without errors.
            serialized = json.dumps(snap, sort_keys=True)
            assert isinstance(serialized, str)
            # Diagnostics sub-section.
            assert "diagnostics" in snap
            json.dumps(snap["diagnostics"])
        finally:
            await clean_stop(app)

    @pytest.mark.asyncio
    async def test_capacity_section(self, tmp_paths: MedrePaths) -> None:
        """Capacity section has delivery and replay counters."""
        config = make_multi_adapter_config()
        app = await build_and_start(config, tmp_paths)
        try:
            snap = build_runtime_snapshot(app)
            cap = snap["capacity"]["state"]
            assert cap is not None
            assert "delivery_current" in cap
            assert "delivery_limit" in cap
            assert "replay_current" in cap
            assert "replay_limit" in cap
        finally:
            await clean_stop(app)
