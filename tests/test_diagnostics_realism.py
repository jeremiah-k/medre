"""Track 6 — snapshot + diagnostics realism validation.

Validates that snapshots, diagnostics, and operator-facing summaries remain
useful, readable, deterministic, and safe under fake-realistic runtime
conditions — including partial adapter failure, capacity pressure, replay
activity, degraded health states, and large error payloads.

All tests compose realistic fake runtime state.  No live adapters, no
network, no async I/O.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pytest

from medre.core.diagnostics.replay_metrics import ReplayMetrics, ReplayRouteCounters
from medre.core.diagnostics.snapshot import build_diagnostics_snapshot
from medre.core.routing.stats import RouteStats
from medre.observability.sanitization import sanitize_error as _sanitize_error
from medre.runtime.boot_summary import BootSummary, build_boot_summary
from medre.runtime.capacity import CapacityController
from medre.runtime.snapshot import (
    SCHEMA_VERSION,
    _MAX_ADAPTERS,
    _MAX_BUILD_FAILURES,
    _MAX_ERROR_DETAIL_LEN,
    _MAX_ROUTES,
    build_runtime_snapshot,
)


# ---------------------------------------------------------------------------
# Reusable fakes (mirrors test_runtime_snapshot.py conventions)
# ---------------------------------------------------------------------------


class _FakeRole(Enum):
    TRANSPORT = "transport"
    PRESENTATION = "presentation"
    HYBRID = "hybrid"


@dataclass
class _FakeCapabilities:
    text: bool = True
    title: bool = False
    replies: str = "native"
    max_text_bytes: int | None = None


class _FakeAdapter:
    """Minimal adapter-like object for snapshot testing."""

    def __init__(
        self,
        adapter_id: str = "test-adapter",
        platform: str = "test_platform",
        role: _FakeRole | None = None,
        version: str = "0.1.0",
        capabilities: _FakeCapabilities | None = None,
        health: str = "unknown",
    ) -> None:
        self.adapter_id = adapter_id
        self.platform = platform
        self.role = role or _FakeRole.TRANSPORT
        self._version = version
        self._capabilities = capabilities or _FakeCapabilities()
        self._last_health = health


@dataclass
class _FakeRuntimeLimits:
    max_inflight_deliveries: int = 50
    max_inflight_replay_events: int = 25
    shutdown_drain_timeout_seconds: int = 10
    delivery_acquire_timeout_seconds: float = 2.0


@dataclass
class _FakeRuntimeConfig:
    limits: Any = field(default_factory=_FakeRuntimeLimits)


class _FakeRouteStats:
    """Mimics RouteStats.snapshot()."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)


class _FakeCapacityController:
    """Mimics CapacityController.snapshot()."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {
            "accepting_work": True,
            "delivery_current": 0,
            "delivery_limit": 50,
            "delivery_rejections": 0,
            "delivery_timeouts": 0,
            "replay_current": 0,
            "replay_limit": 25,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)


class _FakeReplayEngine:
    """Marker object — presence means replay is available."""

    pass


class _FakeDiagnosticsCollector:
    """Mimics DiagnosticsCollector.snapshot()."""

    def __init__(self, replay_data: dict[str, Any] | None = None) -> None:
        self._replay_data = replay_data or {}

    def snapshot(self) -> dict[str, Any]:
        return {"replay": self._replay_data}


class _FakeBuildFailure:
    """Mimics AdapterBuildFailure."""

    def __init__(self, adapter_id: str = "bad-adapter", error: str = "boom") -> None:
        self.adapter_id = adapter_id
        self.error = error


class _FakeRuntimeState(Enum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


_UNSET = object()


# ---------------------------------------------------------------------------
# Fake app builder
# ---------------------------------------------------------------------------


def _make_fake_app(
    *,
    adapters: dict[str, Any] | None = None,
    state: Any = _FakeRuntimeState.RUNNING,
    route_stats: Any = None,
    capacity_controller: Any = None,
    replay_engine: Any = None,
    config: Any = _UNSET,
    build_failures: list[Any] | None = None,
    diagnostics_collector: Any = None,
    startup_wall: str | None = None,
    startup_monotonic: float | None = None,
    health_state: Any = None,
    accounting: Any = None,
    boot_summary: Any = None,
) -> Any:
    """Build a fake app object for testing."""

    @dataclass
    class _FakeApp:
        adapters: dict[str, Any] = field(default_factory=dict)
        state: Any = _FakeRuntimeState.RUNNING
        route_stats: Any = None
        _capacity_controller: Any = None
        _replay_engine: Any = None
        config: Any = field(default_factory=_FakeRuntimeConfig)
        build_failures: list[Any] = field(default_factory=list)
        _diagnostics_collector: Any = None
        _startup_wall: str | None = None
        _startup_monotonic: float | None = None
        _health_state: Any = None
        _runtime_accounting: Any = None
        _boot_summary: Any = None

    app = _FakeApp(
        adapters=adapters or {},
        state=state,
        route_stats=route_stats,
        _capacity_controller=capacity_controller,
        _replay_engine=replay_engine,
        config=config if config is not _UNSET else _FakeRuntimeConfig(),
        build_failures=build_failures or [],
        _diagnostics_collector=diagnostics_collector,
        _startup_wall=startup_wall,
        _startup_monotonic=startup_monotonic,
        _health_state=health_state,
        _runtime_accounting=accounting,
        _boot_summary=boot_summary,
    )
    return app


# Fixed clock for deterministic snapshots
_FIXED_NOW = datetime(2026, 5, 12, 10, 30, 0, tzinfo=timezone.utc)
_FIXED_MONO = 5000.0


def _fixed_now() -> datetime:
    return _FIXED_NOW


# ---------------------------------------------------------------------------
# Realistic scenario builder
# ---------------------------------------------------------------------------


def _build_degraded_runtime_app() -> Any:
    """Build a fake app simulating a partially-failed degraded runtime.

    Scenario:
    - 2 healthy adapters (matrix-1, mesh-1)
    - 1 unhealthy adapter (matrix-2, health=unhealthy)
    - 1 build failure (lxmf-bad, config error with a leaked token in message)
    - Routes with mixed delivery outcomes
    - Capacity under pressure (rejections, timeouts)
    - Replay with backlog, rejections, cancellations
    - Boot summary showing partial startup
    - Health state showing degraded
    """
    # Adapters
    adapters = {
        "matrix-1": _FakeAdapter(
            adapter_id="matrix-1",
            platform="matrix",
            role=_FakeRole.TRANSPORT,
            version="0.3.0",
            health="healthy",
        ),
        "mesh-1": _FakeAdapter(
            adapter_id="mesh-1",
            platform="meshtastic",
            role=_FakeRole.TRANSPORT,
            version="0.2.1",
            health="healthy",
        ),
        "matrix-2": _FakeAdapter(
            adapter_id="matrix-2",
            platform="matrix",
            role=_FakeRole.HYBRID,
            version="0.3.0",
            health="unhealthy",
        ),
    }

    # Route stats with realistic traffic
    rs = RouteStats()
    # Active route with good throughput
    for _ in range(150):
        rs.record_delivered("matrix-to-mesh-general")
    for _ in range(3):
        rs.record_failed(
            "matrix-to-mesh-general",
            "timeout waiting for mesh ack (node ABC123)",
        )
    # Failing route
    for _ in range(7):
        rs.record_failed(
            "matrix-2-to-lxmf-alerts",
            "connection refused by LXMF peer at 10.0.0.5:8080",
        )
    # Loop-prevented route
    for _ in range(12):
        rs.record_delivered("mesh-to-matrix-status")
    for _ in range(4):
        rs.record_loop_prevented("mesh-to-matrix-status")
    # Skipped route
    for _ in range(2):
        rs.record_skipped("matrix-to-mesh-dm-filtered")

    # Build failure with token in error
    build_failures = [
        _FakeBuildFailure(
            adapter_id="lxmf-bad",
            error="ConfigError: access_token=syt_deadbeef12345678 is invalid for homeserver",
        ),
    ]

    # Capacity controller under pressure
    cap_data = {
        "accepting_work": True,
        "delivery_current": 45,
        "delivery_limit": 50,
        "delivery_rejections": 23,
        "delivery_timeouts": 8,
        "replay_current": 20,
        "replay_limit": 25,
        "replay_rejections": 5,
        "replay_timeouts": 2,
    }

    # Replay metrics with realistic activity
    rm = ReplayMetrics()
    for _ in range(50):
        rm.record_events_processed("matrix-to-mesh-general")
    for _ in range(48):
        rm.record_delivery_attempted("matrix-to-mesh-general")
    for _ in range(45):
        rm.record_delivery_succeeded("matrix-to-mesh-general")
    for _ in range(3):
        rm.record_delivery_failed("matrix-to-mesh-general")
    rm.record_skipped_by_filter("matrix-to-mesh-general")
    for _ in range(10):
        rm.record_events_processed("mesh-to-matrix-status")
    for _ in range(10):
        rm.record_delivery_succeeded("mesh-to-matrix-status")
    rm.set_backlog_estimate(137)
    rm.record_rejection()
    rm.record_rejection()
    rm.record_cancellation()

    # Boot summary showing partial startup
    boot = build_boot_summary(
        startup_timestamp="2026-05-12T10:29:55+00:00",
        startup_outcome="partial",
        runtime_health="degraded",
        adapters_started=3,
        adapters_failed=0,
        adapters_total=4,
        adapters_disabled=0,
        build_failure_count=1,
        failed_adapter_ids=[],
        started_adapter_ids=["matrix-1", "matrix-2", "mesh-1"],
        route_count=4,
        storage_backend="sqlite",
        replay_available=True,
        persisted_events_count=2048,
    )

    # Health state — a simple dict representing degraded health
    health_state = {
        "overall": "degraded",
        "healthy_adapters": ["matrix-1", "mesh-1"],
        "unhealthy_adapters": ["matrix-2"],
        "failed_adapters": [],
    }
    # Give it a to_dict method so snapshot picks it up
    health_state_obj = type("_FakeHealthState", (), {"to_dict": lambda self: health_state})()

    return _make_fake_app(
        adapters=adapters,
        state=_FakeRuntimeState.RUNNING,
        route_stats=rs,
        capacity_controller=_FakeCapacityController(cap_data),
        replay_engine=_FakeReplayEngine(),
        build_failures=build_failures,
        diagnostics_collector=_FakeDiagnosticsCollector(rm.snapshot()),
        startup_wall="2026-05-12T10:29:55+00:00",
        startup_monotonic=4995.0,
        health_state=health_state_obj,
        boot_summary=boot,
    )


# =====================================================================
# 1. Snapshot readable structure
# =====================================================================


class TestSnapshotReadability:
    """Full runtime snapshot has navigable, operator-readable structure."""

    def test_top_level_keys_present_and_sorted(self) -> None:
        """Snapshot has all expected top-level keys in alphabetical order."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )

        expected_keys = [
            "accounting",
            "adapters",
            "capacity",
            "diagnostics",
            "health",
            "identity",
            "lifecycle",
            "limits",
            "persistence",
            "replay",
            "retry",
            "routes",
            "schema_version",
            "snapshot_at",
            "startup",
            "unstable",
        ]
        actual_keys = list(snap.keys())
        assert actual_keys == expected_keys, (
            f"Top-level keys mismatch.\nExpected: {expected_keys}\nActual:   {actual_keys}"
        )

    def test_snapshot_is_human_navigable_as_json(self) -> None:
        """Snapshot serializes to indented JSON that an operator can read."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        rendered = json.dumps(snap, indent=2, sort_keys=True)

        # Must be parseable
        parsed = json.loads(rendered)
        assert parsed["schema_version"] == SCHEMA_VERSION
        assert parsed["lifecycle"]["runtime_state"] == "running"

        # Must contain key identifiers an operator would look for
        assert "matrix-1" in rendered
        assert "mesh-1" in rendered
        assert "degraded" in rendered
        assert "partial" in rendered

    def test_adapter_entries_have_standard_shape(self) -> None:
        """Each adapter entry has the same readable key structure."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        for adapter_id, adapter_data in snap["adapters"].items():
            assert "adapter_id" in adapter_data, f"Missing adapter_id in {adapter_id}"
            assert "platform" in adapter_data
            assert "role" in adapter_data
            assert "version" in adapter_data
            assert "health" in adapter_data
            assert "capabilities" in adapter_data
            assert adapter_data["adapter_id"] == adapter_id


# =====================================================================
# 2. Route stats usefulness
# =====================================================================


class TestRouteStatsUsefulness:
    """Route counters provide actionable delivery visibility."""

    def test_per_route_counters_visible_in_snapshot(self) -> None:
        """Routes show delivered/failed/skipped/loop_prevented counts."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        general = snap["routes"]["stats"]["per_route"]["matrix-to-mesh-general"]
        assert general["delivered"] == 150
        assert general["failed"] == 3
        assert general["skipped"] == 0
        assert general["loop_prevented"] == 0
        assert "last_error" in general

    def test_failing_route_shows_last_error(self) -> None:
        """A failing route has its latest error visible and sanitized."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        failing = snap["routes"]["stats"]["per_route"]["matrix-2-to-lxmf-alerts"]
        assert failing["failed"] == 7
        assert "last_error" in failing
        assert "connection refused" in failing["last_error"]
        # Error should NOT contain raw tokens
        assert "syt_" not in failing["last_error"]

    def test_loop_prevented_route_visible(self) -> None:
        """Loop-prevention counters are present and non-zero."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        status_route = snap["routes"]["stats"]["per_route"]["mesh-to-matrix-status"]
        assert status_route["loop_prevented"] == 4
        assert status_route["delivered"] == 12

    def test_route_stats_snapshot_independent(self) -> None:
        """RouteStats.snapshot() independently produces useful output."""
        rs = RouteStats()
        rs.record_delivered("route-a")
        rs.record_delivered("route-a")
        rs.record_failed("route-a", "timeout: channel full")
        rs.record_loop_prevented("route-b")
        snap = rs.snapshot()

        assert snap["route-a"]["delivered"] == 2
        assert snap["route-a"]["failed"] == 1
        assert snap["route-a"]["last_error"] == "timeout: channel full"
        assert snap["route-b"]["loop_prevented"] == 1


# =====================================================================
# 3. Degraded-state clarity
# =====================================================================


class TestDegradedStateClarity:
    """Degraded runtime conditions are clearly visible in snapshots."""

    def test_health_shows_degraded(self) -> None:
        """Health snapshot contains degraded state information."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert snap["startup"]["startup_health"] is not None
        assert snap["startup"]["startup_health"]["overall"] == "degraded"
        assert "matrix-2" in snap["startup"]["startup_health"]["unhealthy_adapters"]

    def test_boot_summary_shows_partial_failure(self) -> None:
        """Boot summary captures partial startup outcome."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        bs = snap["startup"]["boot_summary"]
        assert bs is not None
        assert bs["startup_outcome"] == "partial"
        assert bs["runtime_health"] == "degraded"
        assert bs["build_failure_count"] == 1
        assert bs["adapters_total"] == 4
        assert bs["adapters_started"] == 3

    def test_unhealthy_adapter_marked_in_adapters(self) -> None:
        """Unhealthy adapter is clearly marked in adapter entries."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert snap["adapters"]["matrix-2"]["health"] == "unhealthy"
        assert snap["adapters"]["matrix-1"]["health"] == "healthy"

    def test_build_failure_with_degraded_attribution(self) -> None:
        """Build failures identify which adapter failed and why (sanitized)."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert len(snap["startup"]["build_failures"]) == 1
        bf = snap["startup"]["build_failures"][0]
        assert bf["adapter_id"] == "lxmf-bad"
        # Token in error must be redacted
        assert "syt_deadbeef" not in bf["error"]
        assert "[REDACTED]" in bf["error"]


# =====================================================================
# 4. Startup summary usefulness
# =====================================================================


class TestStartupSummaryUsefulness:
    """Boot summary provides operator-actionable startup information."""

    def test_boot_summary_all_fields_present(self) -> None:
        """Boot summary to_dict has all documented fields."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-12T10:00:00+00:00",
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=2,
            adapters_failed=1,
            adapters_total=3,
            adapters_disabled=0,
            build_failure_count=1,
            failed_adapter_ids=["bad-1"],
            started_adapter_ids=["a", "b"],
            route_count=5,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=100,
        )
        d = bs.to_dict()
        expected_fields = {
            "startup_timestamp",
            "startup_outcome",
            "runtime_health",
            "adapters_started",
            "adapters_failed",
            "adapters_total",
            "adapters_disabled",
            "build_failure_count",
            "build_failure_ids",
            "failed_adapter_ids",
            "started_adapter_ids",
            "route_count",
            "storage_backend",
            "replay_available",
            "persisted_events_count",
        }
        assert set(d.keys()) == expected_fields

    def test_boot_summary_deterministic_json(self) -> None:
        """Boot summary serializes deterministically."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-12T10:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=2,
            adapters_failed=0,
            adapters_total=2,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["z-adapter", "a-adapter"],
            route_count=3,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        d1 = json.dumps(bs.to_dict(), sort_keys=True)
        d2 = json.dumps(bs.to_dict(), sort_keys=True)
        assert d1 == d2

        # Adapter IDs should be sorted
        parsed = json.loads(d1)
        assert parsed["started_adapter_ids"] == ["a-adapter", "z-adapter"]

    def test_boot_summary_in_runtime_snapshot(self) -> None:
        """Boot summary embedded in runtime snapshot is complete."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        bs = snap["startup"]["boot_summary"]
        assert bs is not None
        # Operator can compute success ratio
        assert bs["adapters_total"] > 0
        success_ratio = bs["adapters_started"] / bs["adapters_total"]
        assert 0.0 <= success_ratio <= 1.0
        assert bs["startup_outcome"] in {"success", "partial", "total_failure"}


# =====================================================================
# 5. Replay metrics usefulness
# =====================================================================


class TestReplayMetricsUsefulness:
    """Replay counters provide visibility into replay health."""

    def test_replay_snapshot_global_totals(self) -> None:
        """ReplayMetrics.snapshot() includes global aggregated counters."""
        rm = ReplayMetrics()
        for _ in range(10):
            rm.record_events_processed("route-a")
        for _ in range(8):
            rm.record_delivery_attempted("route-a")
        for _ in range(7):
            rm.record_delivery_succeeded("route-a")
        rm.record_delivery_failed("route-a")
        rm.set_backlog_estimate(50)
        rm.record_rejection()
        rm.record_cancellation()

        snap = rm.snapshot()
        g = snap["global"]
        assert g["replay_events_processed"] == 10
        assert g["replay_deliveries_attempted"] == 8
        assert g["replay_deliveries_succeeded"] == 7
        assert g["replay_deliveries_failed"] == 1
        assert g["backlog_estimate"] == 50
        assert g["rejection_count"] == 1
        assert g["cancellation_count"] == 1
        assert g["last_cancelled_at"] is not None

    def test_replay_snapshot_per_route_breakdown(self) -> None:
        """Per-route replay counters are visible and sorted."""
        rm = ReplayMetrics()
        rm.record_events_processed("route-z")
        rm.record_events_processed("route-a")
        rm.record_delivery_succeeded("route-z")
        rm.record_delivery_failed("route-a")

        snap = rm.snapshot()
        by_route = snap["by_route"]
        assert list(by_route.keys()) == ["route-a", "route-z"]
        assert by_route["route-z"]["events_processed"] == 1
        assert by_route["route-z"]["deliveries_succeeded"] == 1
        assert by_route["route-a"]["deliveries_failed"] == 1

    def test_replay_metrics_in_runtime_snapshot(self) -> None:
        """Replay counters from degraded runtime are present and meaningful."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        replay = snap["replay"]
        assert replay["available"] is True
        counters = replay["counters"]
        assert counters is not None
        assert counters["global"]["backlog_estimate"] == 137
        assert counters["global"]["rejection_count"] == 2
        assert counters["global"]["cancellation_count"] == 1


# =====================================================================
# 6. Capacity metrics usefulness
# =====================================================================


class TestCapacityMetricsUsefulness:
    """Capacity controller snapshots show pressure clearly."""

    def test_capacity_snapshot_fields(self) -> None:
        """CapacityController.snapshot() has all expected fields."""
        from medre.config.model import RuntimeLimits

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

    def test_capacity_under_pressure_in_snapshot(self) -> None:
        """Degraded runtime capacity shows high utilization and rejections."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        cap = snap["capacity"]["state"]
        assert cap is not None
        # Operator can compute utilization
        delivery_util = cap["delivery_current"] / cap["delivery_limit"]
        assert delivery_util == pytest.approx(0.9, abs=0.01)  # 45/50
        assert cap["delivery_rejections"] == 23
        assert cap["delivery_timeouts"] == 8
        assert cap["replay_rejections"] == 5

    def test_capacity_stopped_accepting_work(self) -> None:
        """When stopped, accepting_work=False is visible."""
        cap_data = {
            "accepting_work": False,
            "delivery_current": 3,
            "delivery_limit": 50,
            "delivery_rejections": 100,
            "delivery_timeouts": 10,
            "replay_current": 0,
            "replay_limit": 25,
            "replay_rejections": 50,
            "replay_timeouts": 5,
        }
        app = _make_fake_app(capacity_controller=_FakeCapacityController(cap_data))
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert snap["capacity"]["state"]["accepting_work"] is False


# =====================================================================
# 7. Deterministic formatting
# =====================================================================


class TestDeterministicFormatting:
    """Snapshots are byte-identical when clock is frozen."""

    def test_repeated_snapshots_byte_identical(self) -> None:
        """Two snapshots of the same state with frozen clock are identical."""
        app = _build_degraded_runtime_app()
        snap1 = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        snap2 = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert json.dumps(snap1, sort_keys=True) == json.dumps(
            snap2, sort_keys=True
        )

    def test_key_ordering_stable_at_all_levels(self) -> None:
        """Keys are alphabetically sorted at every nesting level."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        rendered = json.dumps(snap, sort_keys=True)
        # Parse back — json.dumps with sort_keys guarantees order
        parsed = json.loads(rendered)

        def _check_sorted(d: dict, path: str = "root") -> None:
            keys = list(d.keys())
            assert keys == sorted(keys), f"Keys not sorted at {path}: {keys}"

        _check_sorted(parsed)
        for k, v in parsed.items():
            if isinstance(v, dict):
                _check_sorted(v, path=k)

    def test_diagnostics_snapshot_deterministic(self) -> None:
        """build_diagnostics_snapshot output is deterministic."""
        rs = RouteStats()
        rs.record_delivered("route-a")
        rs.record_failed("route-b", "error")

        rm = ReplayMetrics()
        rm.record_events_processed("route-a")
        rm.set_backlog_estimate(10)

        cap = {"delivery_current": 5, "delivery_limit": 50}

        snap1 = build_diagnostics_snapshot(rs, rm, capacity_snapshot=cap)
        snap2 = build_diagnostics_snapshot(rs, rm, capacity_snapshot=cap)
        assert json.dumps(snap1, sort_keys=True) == json.dumps(
            snap2, sort_keys=True
        )


# =====================================================================
# 8. Bounded truncation clarity
# =====================================================================


class TestBoundedTruncation:
    """Truncated values show visible truncation indicators."""

    def test_long_route_error_truncated_with_ellipsis(self) -> None:
        """Errors exceeding 512 chars are truncated with '...' suffix."""
        # Use spaces and punctuation to avoid matching base64-like token patterns
        long_error = "Connection timeout: " + "retry failed. " * 60
        sanitized = _sanitize_error(long_error)
        assert len(sanitized) <= 512
        assert sanitized.endswith("...")

    def test_long_build_failure_error_truncated(self) -> None:
        """Build failure errors exceeding the limit are truncated with '...'."""
        # Use spaces and punctuation to avoid matching token patterns
        long_error = "Build failure: " + "dependency error. " * 40
        bf = _FakeBuildFailure(adapter_id="test-adapter", error=long_error)
        app = _make_fake_app(build_failures=[bf])
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        error_val = snap["startup"]["build_failures"][0]["error"]
        assert len(error_val) <= _MAX_ERROR_DETAIL_LEN
        assert error_val.endswith("...")

    def test_truncation_preserves_prefix_information(self) -> None:
        """Truncated errors retain the informative prefix."""
        meaningful_prefix = "ConnectionError: timeout after 30s to host 10.0.0.5:8080"
        padding = " pad" * 200  # push past 512 chars
        long_error = meaningful_prefix + padding
        sanitized = _sanitize_error(long_error)
        assert sanitized.startswith(meaningful_prefix[:50])
        assert sanitized.endswith("...")

    def test_build_failures_bounded(self) -> None:
        """Build failures list is capped at _MAX_BUILD_FAILURES."""
        failures = [
            _FakeBuildFailure(adapter_id=f"bf-{i}", error=f"error-{i}")
            for i in range(_MAX_BUILD_FAILURES + 20)
        ]
        app = _make_fake_app(build_failures=failures)
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert len(snap["startup"]["build_failures"]) == _MAX_BUILD_FAILURES


# =====================================================================
# 9. Degraded attribution
# =====================================================================


class TestDegradedAttribution:
    """Operator can identify which adapters/routes caused degradation."""

    def test_failed_route_attributed_to_specific_route(self) -> None:
        """Failing routes have non-zero failed count and last_error."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        # Operator can find the failing route
        failing_routes = {
            rid: r for rid, r in snap["routes"]["stats"]["per_route"].items() if r.get("failed", 0) > 0
        }
        assert "matrix-2-to-lxmf-alerts" in failing_routes
        assert "last_error" in failing_routes["matrix-2-to-lxmf-alerts"]

    def test_unhealthy_adapter_identifiable(self) -> None:
        """Unhealthy adapters are identifiable by health field."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        unhealthy = {
            aid: a for aid, a in snap["adapters"].items() if a["health"] != "healthy"
        }
        assert "matrix-2" in unhealthy
        assert unhealthy["matrix-2"]["health"] == "unhealthy"

    def test_build_failure_attributed_to_adapter(self) -> None:
        """Build failures identify the specific adapter that failed."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        bf = snap["startup"]["build_failures"][0]
        assert bf["adapter_id"] == "lxmf-bad"
        assert "error" in bf
        assert len(bf["error"]) > 0

    def test_boot_summary_attributes_failure_count(self) -> None:
        """Boot summary clearly shows how many adapters failed to build."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        bs = snap["startup"]["boot_summary"]
        assert bs["build_failure_count"] == 1
        assert bs["adapters_total"] == 4
        assert bs["adapters_started"] == 3


# =====================================================================
# 10. JSON-safe export validation
# =====================================================================


class TestJsonSafeExport:
    """Snapshots contain no SDK objects, secrets, or non-serializable values."""

    def test_full_snapshot_json_serializable(self) -> None:
        """Entire degraded-runtime snapshot is JSON-serializable."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        serialized = json.dumps(snap, sort_keys=True)
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_no_sdk_objects_in_snapshot(self) -> None:
        """Snapshot values contain no SDK objects (no angle-bracket reprs)."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        rendered = json.dumps(snap)

        # SDK objects typically show as "<module.Class object at 0x...>"
        assert " object at 0x" not in rendered
        assert "<" not in rendered or "[OBJECT_REPR]" in rendered

    def test_no_secrets_in_snapshot(self) -> None:
        """Tokens and secrets in errors are redacted."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        rendered = json.dumps(snap)

        # The build failure contains "access_token=syt_deadbeef12345678"
        # which should be redacted
        assert "syt_deadbeef" not in rendered
        assert "access_token=" not in rendered

    def test_only_json_safe_types(self) -> None:
        """All leaf values are JSON-safe types (str, int, float, bool, None)."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )

        def _check_leaf(value: Any, path: str = "root") -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    _check_leaf(v, path=f"{path}.{k}")
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    _check_leaf(v, path=f"{path}[{i}]")
            else:
                assert isinstance(value, (str, int, float, bool, type(None))), (
                    f"Non-JSON-safe type at {path}: {type(value).__name__} = {value!r}"
                )

        _check_leaf(snap)

    def test_diagnostics_snapshot_json_safe(self) -> None:
        """build_diagnostics_snapshot output is fully JSON-serializable."""
        rs = RouteStats()
        rs.record_delivered("r1")
        rs.record_failed("r2", "error with syt_secret123 token")

        rm = ReplayMetrics()
        rm.record_events_processed("r1")
        rm.set_backlog_estimate(42)

        snap = build_diagnostics_snapshot(
            rs, rm, capacity_snapshot={"delivery_current": 1, "delivery_limit": 10}
        )
        serialized = json.dumps(snap)
        parsed = json.loads(serialized)
        assert "routes" in parsed
        assert "replay" in parsed
        assert "capacity" in parsed


# =====================================================================
# 11. Combined diagnostics snapshot (RouteStats + ReplayMetrics + capacity)
# =====================================================================


class TestCombinedDiagnosticsSnapshot:
    """build_diagnostics_snapshot composes all subsystems correctly."""

    def test_combined_snapshot_structure(self) -> None:
        """Combined snapshot has routes, replay, and capacity keys."""
        rs = RouteStats()
        rs.record_delivered("route-x")
        rs.record_failed("route-y", "fail")

        rm = ReplayMetrics()
        rm.record_events_processed("route-x")
        rm.set_backlog_estimate(99)

        cap = {
            "accepting_work": True,
            "delivery_current": 10,
            "delivery_limit": 50,
            "delivery_rejections": 0,
            "delivery_timeouts": 0,
            "replay_current": 0,
            "replay_limit": 25,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }

        snap = build_diagnostics_snapshot(rs, rm, capacity_snapshot=cap)
        assert "routes" in snap
        assert "replay" in snap
        assert "capacity" in snap

        # Routes from RouteStats
        assert "route-x" in snap["routes"]
        assert snap["routes"]["route-x"]["delivered"] == 1

        # Replay from ReplayMetrics
        assert snap["replay"]["global"]["backlog_estimate"] == 99

        # Capacity
        assert snap["capacity"]["delivery_current"] == 10

    def test_combined_without_capacity(self) -> None:
        """Combined snapshot works without capacity_snapshot."""
        rs = RouteStats()
        rm = ReplayMetrics()
        snap = build_diagnostics_snapshot(rs, rm)
        assert "capacity" not in snap
        assert "routes" in snap
        assert "replay" in snap


# =====================================================================
# 12. Uptime and timestamp realism
# =====================================================================


class TestUptimeTimestampRealism:
    """Startup timestamps and uptime are operator-meaningful."""

    def test_uptime_computed_correctly(self) -> None:
        """Uptime is (monotonic_now - startup_monotonic), rounded."""
        app = _make_fake_app(
            startup_wall="2026-05-12T10:00:00+00:00",
            startup_monotonic=5000.0,
        )
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: 5042.5
        )
        assert snap["lifecycle"]["uptime_seconds"] == pytest.approx(42.5, abs=0.001)
        assert snap["lifecycle"]["startup_timestamp"] == "2026-05-12T10:00:00+00:00"

    def test_uptime_null_before_startup(self) -> None:
        """Before startup, uptime and startup_timestamp are null."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        assert snap["lifecycle"]["uptime_seconds"] is None
        assert snap["lifecycle"]["startup_timestamp"] is None

    def test_snapshot_at_is_iso_format(self) -> None:
        """snapshot_at is a valid ISO-8601 timestamp."""
        app = _build_degraded_runtime_app()
        snap = build_runtime_snapshot(
            app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO
        )
        # Should parse without error
        parsed_dt = datetime.fromisoformat(snap["snapshot_at"])
        assert parsed_dt.tzinfo is not None  # timezone-aware


# =====================================================================
# 13. RouteStats error sanitization realism
# =====================================================================


class TestErrorSanitizationRealism:
    """Error sanitization handles realistic error payloads."""

    def test_matrix_token_redacted(self) -> None:
        """Matrix-style access tokens are redacted."""
        err = _sanitize_error("Failed with token syt_abcdef123456 for user")
        assert "syt_abcdef" not in err
        assert "[REDACTED]" in err

    def test_openai_key_redacted(self) -> None:
        """OpenAI-style API keys are redacted."""
        err = _sanitize_error("API call failed with key sk-abc123def456ghi789jkl012mno")
        assert "sk-abc123" not in err
        assert "[REDACTED]" in err

    def test_password_pattern_redacted(self) -> None:
        """Password patterns are redacted."""
        err = _sanitize_error('Connection failed: password=supersecret123')
        assert "supersecret123" not in err
        assert "[REDACTED]" in err

    def test_sdk_repr_removed(self) -> None:
        """Raw SDK object repr strings are replaced."""
        err = _sanitize_error(
            "Got <nio.responses.SyncResponse object at 0x7f1234567890> from sync"
        )
        assert "object at 0x" not in err
        assert "[OBJECT_REPR]" in err

    def test_long_base64_redacted(self) -> None:
        """Long base64-like strings (tokens/keys) are redacted."""
        token = "QWJjZGVmZ2hpamtsbW5vcHFydXN0dnd4eXoxMjM0NTY3ODkw"  # realistic base64, 46 chars
        err = _sanitize_error(f"Failed with key {token}")
        assert token not in err
        assert "[REDACTED]" in err

    def test_normal_errors_preserved(self) -> None:
        """Normal, safe error messages pass through unmodified."""
        msg = "Connection refused by LXMF peer at 10.0.0.5:8080"
        assert _sanitize_error(msg) == msg
