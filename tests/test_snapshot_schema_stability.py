"""Track 7 — snapshot + diagnostics schema stability validation.

Validates that all snapshot and diagnostics export surfaces remain
deterministic, well-shaped, bounded, JSON-safe, and free of secrets
without formally freezing schema versions.  Tests operate on the
real runtime classes — no mocks for the components under test.

Coverage areas
--------------
1. Schema consistency — every snapshot returns the expected key set.
2. Deterministic ordering — keys and entries are alphabetically sorted.
3. Bounded exports — collections respect documented size caps.
4. Malformed-adapter resilience — broken inputs degrade gracefully.
5. Replay/capacity consistency — global totals match per-route sums;
   capacity counters reflect internal state.
6. Route-stat consistency — counter shapes are uniform across routes.
7. JSON-safe exports — ``json.dumps`` succeeds on every snapshot type.
8. No secret leakage — sanitised errors and diagnostic contract drop
   secrets.

All tests are synchronous, deterministic, and bounded.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pytest

from medre.config.model import RuntimeLimits
from medre.core.diagnostics.replay_metrics import ReplayMetrics
from medre.core.diagnostics.snapshot import build_diagnostics_snapshot
from medre.core.observability.sanitization import sanitize_error as _sanitize_error
from medre.core.routing.stats import RouteStats
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.core.supervision.diagnostic_contract import (
    COMMON_DIAGNOSTIC_KEYS,
    normalize_diagnostics,
)
from medre.runtime.snapshot import (
    _MAX_ADAPTERS,
    _MAX_BUILD_FAILURES,
    _MAX_ERROR_DETAIL_LEN,
    _MAX_ROUTES,
    SCHEMA_VERSION,
    build_runtime_snapshot,
)

# ---------------------------------------------------------------------------
# Reusable fakes (follows test_runtime_snapshot.py conventions)
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


class _FakeDiagnosticAdapter(_FakeAdapter):
    """Adapter that exposes a synchronous diagnostics() method."""

    def __init__(self, diag_data: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._diag_data = diag_data or {"connected": True, "health": "healthy"}

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._diag_data)


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
        config: Any = None
        build_failures: list[Any] = field(default_factory=list)
        _diagnostics_collector: Any = None
        diagnostician: Any = None
        _startup_wall: str | None = None
        _startup_monotonic: float | None = None
        _health_state: Any = None
        _runtime_accounting: Any = None
        _boot_summary: Any = None

    return _FakeApp(
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


# ---------------------------------------------------------------------------
# Expected key sets (single source of truth for shape validation)
# ---------------------------------------------------------------------------

_EXPECTED_ROUTE_ENTRY_KEYS: frozenset[str] = frozenset(
    {"delivered", "failed", "skipped", "loop_prevented", "policy_suppressed"}
)

_EXPECTED_REPLAY_GLOBAL_KEYS: frozenset[str] = frozenset(
    {
        "replay_events_processed",
        "replay_deliveries_attempted",
        "replay_deliveries_succeeded",
        "replay_deliveries_failed",
        "replay_skipped_by_filter",
        "replay_skipped_by_loop",
        "backlog_estimate",
        "rejection_count",
        "cancellation_count",
        "last_cancelled_at",
    }
)

_EXPECTED_REPLAY_ROUTE_KEYS: frozenset[str] = frozenset(
    {
        "events_processed",
        "deliveries_attempted",
        "deliveries_succeeded",
        "deliveries_failed",
        "skipped_by_filter",
        "skipped_by_loop",
    }
)

_EXPECTED_CAPACITY_KEYS: frozenset[str] = frozenset(
    {
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
)

_EXPECTED_ACCOUNTING_KEYS: frozenset[str] = frozenset(
    {
        "capacity_rejections",
        "inbound_accepted",
        "loop_prevented",
        "outbound_attempts",
        "outbound_delivered",
        "outbound_failed",
        "policy_suppressed",
        "replay_processed",
        "replay_rejected",
    }
)

_EXPECTED_RUNTIME_SNAPSHOT_TOP_KEYS: frozenset[str] = frozenset(
    {
        "accounting",
        "adapters",
        "capacity",
        "diagnostics",
        "health",
        "identity",
        "lifecycle",
        "limits",
        "outbox",
        "persistence",
        "replay",
        "retry",
        "routes",
        "schema_version",
        "snapshot_at",
        "snapshot_scope",
        "startup",
        "unstable",
    }
)

_EXPECTED_DIAG_SNAPSHOT_KEYS: frozenset[str] = frozenset({"routes", "replay"})

# ---------------------------------------------------------------------------
# Tranche 3 — expected-key constants for diagnostics schema.
# ---------------------------------------------------------------------------

_EXPECTED_RETRY_KEYS: frozenset[str] = frozenset(
    {
        "dead_lettered",
        "enabled",
        "failed",
        "last_run_at",
        "live_refresh",
        "processed",
        "running",
        "scope",
        "succeeded",
    }
)

_EXPECTED_PIPELINE_DIAGNOSTICS_KEYS: frozenset[str] = frozenset({"running"})

# Diagnostics section top-level keys.
_EXPECTED_DIAGNOSTICS_SECTION_KEYS: frozenset[str] = frozenset(
    {
        "adapters",
        "live_refresh",
        "pipeline",
        "runtime_events",
        "scope",
    }
)

# Adapter diagnostics entries always carry the common diagnostic keys
# (from normalize_diagnostics) plus the "adapter" key when adapter_hint
# is supplied.  "transport_specific" appears conditionally.
_EXPECTED_ADAPTER_DIAGNOSTICS_KEYS: frozenset[str] = COMMON_DIAGNOSTIC_KEYS | frozenset(
    {"adapter"}
)


# ===================================================================
# 1. Schema consistency
# ===================================================================


class TestRouteStatsSchemaConsistency:
    """RouteStats.snapshot() returns a stable, uniform key structure."""

    def test_empty_snapshot_is_empty_dict(self) -> None:
        rs = RouteStats()
        assert rs.snapshot() == {}

    def test_single_route_entry_has_expected_counter_keys(self) -> None:
        rs = RouteStats()
        rs.record_delivered("route-a")
        snap = rs.snapshot()
        assert set(snap.keys()) == {"route-a"}
        entry = snap["route-a"]
        assert _EXPECTED_ROUTE_ENTRY_KEYS <= set(entry.keys())

    def test_failed_route_includes_last_error(self) -> None:
        rs = RouteStats()
        rs.record_failed("route-x", "timeout")
        snap = rs.snapshot()
        assert "last_error" in snap["route-x"]
        assert snap["route-x"]["last_error"] == "timeout"

    def test_all_counter_values_are_int(self) -> None:
        rs = RouteStats()
        rs.record_delivered("r1")
        rs.record_failed("r1", "err")
        rs.record_skipped("r1")
        rs.record_loop_prevented("r1")
        entry = rs.snapshot()["r1"]
        for key in _EXPECTED_ROUTE_ENTRY_KEYS:
            assert isinstance(entry[key], int), f"{key} should be int"

    def test_multiple_routes_each_have_consistent_keys(self) -> None:
        rs = RouteStats()
        rs.record_delivered("alpha")
        rs.record_failed("beta", "err")
        rs.record_skipped("gamma")
        snap = rs.snapshot()
        for route_id, entry in snap.items():
            assert _EXPECTED_ROUTE_ENTRY_KEYS <= set(
                entry.keys()
            ), f"route {route_id} missing expected keys"


class TestReplayMetricsSchemaConsistency:
    """ReplayMetrics.snapshot() returns stable global and per-route keys."""

    def test_empty_snapshot_has_global_and_by_route(self) -> None:
        rm = ReplayMetrics()
        snap = rm.snapshot()
        assert set(snap.keys()) == {"global", "by_route"}
        assert snap["by_route"] == {}

    def test_global_section_has_expected_keys(self) -> None:
        rm = ReplayMetrics()
        rm.record_events_processed("r1")
        snap = rm.snapshot()
        assert _EXPECTED_REPLAY_GLOBAL_KEYS <= set(snap["global"].keys())

    def test_per_route_entry_has_expected_keys(self) -> None:
        rm = ReplayMetrics()
        rm.record_events_processed("r1")
        rm.record_delivery_attempted("r1")
        rm.record_delivery_succeeded("r1")
        rm.record_delivery_failed("r1")
        rm.record_skipped_by_filter("r1")
        rm.record_skipped_by_loop("r1")
        snap = rm.snapshot()
        entry = snap["by_route"]["r1"]
        assert set(entry.keys()) == _EXPECTED_REPLAY_ROUTE_KEYS

    def test_all_values_in_global_are_numeric_or_none(self) -> None:
        rm = ReplayMetrics()
        rm.record_events_processed("r1")
        rm.set_backlog_estimate(5)
        rm.record_rejection()
        rm.record_cancellation()
        global_snap = rm.snapshot()["global"]
        for key in _EXPECTED_REPLAY_GLOBAL_KEYS:
            val = global_snap[key]
            if key == "last_cancelled_at":
                assert val is None or isinstance(
                    val, float
                ), f"{key} should be float|None"
            else:
                assert isinstance(val, int), f"{key} should be int"


class TestDiagnosticsSnapshotSchemaConsistency:
    """build_diagnostics_snapshot returns expected top-level keys."""

    def test_minimal_snapshot_has_routes_and_replay(self) -> None:
        rs = RouteStats()
        rm = ReplayMetrics()
        snap = build_diagnostics_snapshot(rs, rm)
        assert set(snap.keys()) == {"routes", "replay"}

    def test_with_capacity_includes_capacity_key(self) -> None:
        rs = RouteStats()
        rm = ReplayMetrics()
        cap = {"accepting_work": True, "delivery_current": 0}
        snap = build_diagnostics_snapshot(rs, rm, capacity_snapshot=cap)
        assert set(snap.keys()) == {"routes", "replay", "capacity"}
        assert snap["capacity"] == cap

    def test_none_capacity_omits_key(self) -> None:
        rs = RouteStats()
        rm = ReplayMetrics()
        snap = build_diagnostics_snapshot(rs, rm, capacity_snapshot=None)
        assert "capacity" not in snap


class TestAccountingSchemaConsistency:
    """RuntimeAccounting.snapshot() has exactly 9 sorted keys."""

    def test_empty_snapshot_has_expected_keys(self) -> None:
        acc = RuntimeAccounting()
        snap = acc.snapshot()
        assert set(snap.keys()) == _EXPECTED_ACCOUNTING_KEYS

    def test_all_values_are_int(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        snap = acc.snapshot()
        for key, val in snap.items():
            assert isinstance(val, int), f"{key} should be int"

    def test_snapshot_and_to_dict_are_identical(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_capacity_rejection()
        assert acc.snapshot() == acc.to_dict()


class TestCapacitySnapshotSchemaConsistency:
    """CapacityController.snapshot() has exactly the expected 9 keys."""

    def test_snapshot_has_expected_keys(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        snap = ctrl.snapshot()
        assert set(snap.keys()) == _EXPECTED_CAPACITY_KEYS

    def test_all_values_are_correct_types(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        snap = ctrl.snapshot()
        assert isinstance(snap["accepting_work"], bool)
        for key in _EXPECTED_CAPACITY_KEYS - {"accepting_work"}:
            assert isinstance(snap[key], int), f"{key} should be int"


class TestRuntimeSnapshotSchemaConsistency:
    """build_runtime_snapshot top-level keys are stable."""

    def test_full_snapshot_has_expected_top_keys(self) -> None:
        fixed_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        fixed_mono = 1000.0
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter()},
            route_stats=_FakeRouteStats(
                {"r1": {"delivered": 1, "failed": 0, "skipped": 0, "loop_prevented": 0}}
            ),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {}}),
            startup_wall="2025-01-01T00:00:00+00:00",
            startup_monotonic=999.0,
            accounting=RuntimeAccounting(),
        )
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_dt,
            monotonic_fn=lambda: fixed_mono,
        )
        assert set(snap.keys()) == _EXPECTED_RUNTIME_SNAPSHOT_TOP_KEYS

    def test_schema_version_is_one(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["schema_version"] == SCHEMA_VERSION
        assert isinstance(snap["schema_version"], int)

    def test_live_health_is_always_null(self) -> None:
        """live_health is null because active health polling is not implemented."""
        app = _make_fake_app(
            health_state={"overall": "healthy"},
        )
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is None

    def test_startup_health_reflects_health_state(self) -> None:
        """startup_health carries the startup-derived supervision snapshot."""
        app = _make_fake_app(
            health_state={"overall": "degraded", "adapters": 2},
        )
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] == {
            "overall": "degraded",
            "adapters": 2,
        }

    def test_startup_health_null_when_absent(self) -> None:
        """startup_health is null when no health state is wired."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] is None


class TestDiagnosticsSectionSchemaStability:
    """Diagnostics section shape and retry sub-shape are stable."""

    def test_diagnostics_section_has_expected_keys(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert set(snap["diagnostics"].keys()) == _EXPECTED_DIAGNOSTICS_SECTION_KEYS

    def test_diagnostics_pipeline_has_expected_keys(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert (
            set(snap["diagnostics"]["pipeline"].keys())
            == _EXPECTED_PIPELINE_DIAGNOSTICS_KEYS
        )

    def test_diagnostics_pipeline_running_is_bool_or_none(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        val = snap["diagnostics"]["pipeline"]["running"]
        assert val is None or isinstance(val, bool)

    def test_diagnostics_scope_is_process_local(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["diagnostics"]["scope"] == "process_local"

    def test_diagnostics_live_refresh_is_false(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["diagnostics"]["live_refresh"] is False

    def test_diagnostics_adapters_is_dict(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert isinstance(snap["diagnostics"]["adapters"], dict)

    def test_diagnostics_runtime_events_is_dict_or_none(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        val = snap["diagnostics"]["runtime_events"]
        assert val is None or isinstance(val, dict)

    def test_adapter_diagnostics_entry_has_expected_keys(self) -> None:
        """Adapter that exposes diagnostics() produces normalised entry."""
        adapter = _FakeDiagnosticAdapter(
            adapter_id="diag-adapter",
            diag_data={"connected": True, "health": "healthy", "latency_ms": 42},
        )
        app = _make_fake_app(adapters={"diag-adapter": adapter})
        snap = build_runtime_snapshot(app)
        adapter_diag = snap["diagnostics"]["adapters"]["diag-adapter"]
        assert set(adapter_diag.keys()) == _EXPECTED_ADAPTER_DIAGNOSTICS_KEYS | {
            "transport_specific"
        }
        # Non-common keys land in transport_specific.
        assert "transport_specific" in adapter_diag
        assert adapter_diag["transport_specific"]["latency_ms"] == 42

    def test_adapter_diagnostics_entry_from_error_has_error_key(self) -> None:
        """Adapter whose diagnostics() raises produces error entry."""

        class _ErrorAdapter(_FakeAdapter):
            def diagnostics(self) -> None:
                raise RuntimeError("boom")

        app = _make_fake_app(adapters={"err-adapter": _ErrorAdapter()})
        snap = build_runtime_snapshot(app)
        err_diag = snap["diagnostics"]["adapters"]["err-adapter"]
        assert "error" in err_diag
        assert "status" in err_diag

    def test_adapter_without_diagnostics_omitted_from_section(self) -> None:
        """Adapter without diagnostics() is silently omitted."""
        adapter = _FakeAdapter(adapter_id="plain-adapter")
        app = _make_fake_app(adapters={"plain-adapter": adapter})
        snap = build_runtime_snapshot(app)
        assert "plain-adapter" not in snap["diagnostics"]["adapters"]


class TestRetrySchemaStability:
    """Retry section key shape is stable."""

    def test_retry_has_expected_keys(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert set(snap["retry"].keys()) == _EXPECTED_RETRY_KEYS

    def test_retry_default_values(self) -> None:
        app = _make_fake_app()
        retry = build_runtime_snapshot(app)["retry"]
        assert retry["enabled"] is False
        assert retry["running"] is False
        assert retry["processed"] == 0
        assert retry["succeeded"] == 0
        assert retry["failed"] == 0
        assert retry["dead_lettered"] == 0
        assert retry["last_run_at"] is None
        assert retry["scope"] == "process_local"
        assert retry["live_refresh"] is False

    def test_retry_values_are_correct_types(self) -> None:
        app = _make_fake_app()
        retry = build_runtime_snapshot(app)["retry"]
        assert isinstance(retry["enabled"], bool)
        assert isinstance(retry["running"], bool)
        assert isinstance(retry["processed"], int)
        assert isinstance(retry["succeeded"], int)
        assert isinstance(retry["failed"], int)
        assert isinstance(retry["dead_lettered"], int)
        assert retry["last_run_at"] is None or isinstance(retry["last_run_at"], str)
        assert isinstance(retry["scope"], str)
        assert isinstance(retry["live_refresh"], bool)


# ===================================================================
# 2. Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Snapshot keys and entries are sorted alphabetically."""

    def test_route_stats_keys_are_sorted(self) -> None:
        rs = RouteStats()
        for rid in ["z-route", "a-route", "m-route"]:
            rs.record_delivered(rid)
        keys = list(rs.snapshot().keys())
        assert keys == sorted(keys)

    def test_replay_by_route_keys_are_sorted(self) -> None:
        rm = ReplayMetrics()
        for rid in ["z-route", "a-route", "m-route"]:
            rm.record_events_processed(rid)
        keys = list(rm.snapshot()["by_route"].keys())
        assert keys == sorted(keys)

    def test_accounting_keys_are_sorted(self) -> None:
        acc = RuntimeAccounting()
        keys = list(acc.snapshot().keys())
        assert keys == sorted(keys)

    def test_capacity_keys_are_sorted(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        keys = list(ctrl.snapshot().keys())
        assert keys == sorted(keys)

    def test_sequential_snapshots_are_identical(self) -> None:
        """Two snapshots from the same unmodified state must be equal."""
        rs = RouteStats()
        rm = ReplayMetrics()
        for rid in ["b", "a", "c"]:
            rs.record_delivered(rid)
            rm.record_events_processed(rid)
        snap1 = build_diagnostics_snapshot(rs, rm)
        snap2 = build_diagnostics_snapshot(rs, rm)
        assert snap1 == snap2

    def test_runtime_snapshot_deterministic_with_fixed_clock(self) -> None:
        fixed_dt = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        fixed_mono = 5000.0
        app = _make_fake_app(
            adapters={"b": _FakeAdapter("b"), "a": _FakeAdapter("a")},
            startup_monotonic=4999.0,
        )
        snap1 = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_dt,
            monotonic_fn=lambda: fixed_mono,
        )
        snap2 = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_dt,
            monotonic_fn=lambda: fixed_mono,
        )
        assert snap1 == snap2

    def test_adapter_ordering_in_runtime_snapshot(self) -> None:
        """Adapter keys in runtime snapshot are alphabetically sorted."""
        fixed_dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        app = _make_fake_app(
            adapters={
                "z-adapter": _FakeAdapter("z-adapter"),
                "a-adapter": _FakeAdapter("a-adapter"),
                "m-adapter": _FakeAdapter("m-adapter"),
            },
        )
        snap = build_runtime_snapshot(app, now_fn=lambda: fixed_dt)
        adapter_keys = list(snap["adapters"].keys())
        assert adapter_keys == sorted(adapter_keys)


# ===================================================================
# 3. Bounded exports
# ===================================================================


class TestBoundedExports:
    """Runtime snapshot enforces size caps on collections."""

    def _make_large_route_stats(self, n: int) -> RouteStats:
        rs = RouteStats()
        for i in range(n):
            rs.record_delivered(f"route-{i:06d}")
        return rs

    def test_routes_capped_at_max(self) -> None:
        n = _MAX_ROUTES + 10
        rs = self._make_large_route_stats(n)
        app = _make_fake_app(route_stats=rs)
        snap = build_runtime_snapshot(app)
        assert len(snap["routes"]["stats"]["per_route"]) <= _MAX_ROUTES

    def test_adapters_capped_at_max(self) -> None:
        adapters = {
            f"adapter-{i:04d}": _FakeAdapter(f"adapter-{i:04d}")
            for i in range(_MAX_ADAPTERS + 10)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app)
        assert len(snap["adapters"]) <= _MAX_ADAPTERS

    def test_build_failures_capped_at_max(self) -> None:
        failures = [
            _FakeBuildFailure(adapter_id=f"bf-{i}", error=f"error {i}")
            for i in range(_MAX_BUILD_FAILURES + 10)
        ]
        app = _make_fake_app(build_failures=failures)
        snap = build_runtime_snapshot(app)
        assert len(snap["startup"]["build_failures"]) <= _MAX_BUILD_FAILURES

    def test_error_strings_truncated_at_max_len(self) -> None:
        long_error = "x" * (_MAX_ERROR_DETAIL_LEN + 100)
        sanitized = _sanitize_error(long_error)
        assert len(sanitized) <= _MAX_ERROR_DETAIL_LEN

    def test_route_stats_snapshot_not_bounded_by_default(self) -> None:
        """RouteStats.snapshot() itself is unbounded — bounding is at the
        runtime snapshot level.  Verify that the raw snapshot includes
        all routes even above _MAX_ROUTES."""
        rs = RouteStats()
        for i in range(_MAX_ROUTES + 5):
            rs.record_delivered(f"r-{i}")
        raw_snap = rs.snapshot()
        assert len(raw_snap) == _MAX_ROUTES + 5


# ===================================================================
# 4. Malformed-adapter resilience
# ===================================================================


class _BrokenAdapter:
    """Adapter whose attribute access raises."""

    @property
    def adapter_id(self) -> str:
        raise RuntimeError("broken")

    @property
    def platform(self) -> str:
        raise RuntimeError("broken")


class _MinimalObject:
    """Object with no adapter attributes at all."""

    pass


class TestMalformedAdapterResilience:
    """Snapshot builders degrade gracefully on broken inputs."""

    def test_runtime_snapshot_handles_broken_adapter(self) -> None:
        app = _make_fake_app(adapters={"broken": _BrokenAdapter()})
        snap = build_runtime_snapshot(app)
        assert "adapters" in snap
        assert "broken" in snap["adapters"]
        # Should have degraded gracefully — adapter_id becomes "unknown"
        assert snap["adapters"]["broken"]["adapter_id"] == "unknown"

    def test_runtime_snapshot_handles_minimal_object(self) -> None:
        app = _make_fake_app(adapters={"minimal": _MinimalObject()})
        snap = build_runtime_snapshot(app)
        assert "adapters" in snap
        assert "minimal" in snap["adapters"]

    def test_normalize_diagnostics_with_none_input(self) -> None:
        result = normalize_diagnostics(None)
        # Should produce common keys with None fallbacks
        assert isinstance(result, dict)
        for key in COMMON_DIAGNOSTIC_KEYS:
            assert key in result

    def test_normalize_diagnostics_with_empty_dict(self) -> None:
        result = normalize_diagnostics({})
        assert isinstance(result, dict)
        for key in COMMON_DIAGNOSTIC_KEYS:
            assert key in result
            assert result[key] is None

    def test_normalize_diagnostics_with_exception_object(self) -> None:
        result = normalize_diagnostics(ValueError("oops"))
        assert isinstance(result, dict)
        # Should not crash — produces common keys
        for key in COMMON_DIAGNOSTIC_KEYS:
            assert key in result

    def test_normalize_diagnostics_with_non_string_health(self) -> None:
        result = normalize_diagnostics({"health": 42, "connected": True})
        assert isinstance(result, dict)
        # 42 should be preserved as-is (safe scalar)
        assert result["health"] == 42

    def test_runtime_snapshot_handles_missing_config(self) -> None:
        app = _make_fake_app(config=None)
        snap = build_runtime_snapshot(app)
        assert "limits" in snap
        assert isinstance(snap["limits"], dict)

    def test_runtime_snapshot_handles_all_missing_subsystems(self) -> None:
        """App with only state — no adapters, no stats, nothing."""
        app = _make_fake_app(
            config=None,
            adapters={},
            route_stats=None,
            capacity_controller=None,
            replay_engine=None,
            diagnostics_collector=None,
        )
        snap = build_runtime_snapshot(app)
        assert snap["adapters"] == {}
        assert snap["routes"]["stats"]["per_route"] == {}
        assert snap["capacity"]["state"] is None
        assert snap["accounting"]["counters"] is None


# ===================================================================
# 5. Replay / capacity consistency
# ===================================================================


class TestReplayConsistency:
    """ReplayMetrics global totals must match per-route sums."""

    def test_global_sums_match_per_route(self) -> None:
        rm = ReplayMetrics()
        for rid in ["r1", "r2", "r3"]:
            rm.record_events_processed(rid)
            rm.record_events_processed(rid)
            rm.record_delivery_attempted(rid)
            rm.record_delivery_succeeded(rid)
            rm.record_delivery_failed(rid)
            rm.record_skipped_by_filter(rid)
            rm.record_skipped_by_loop(rid)
        snap = rm.snapshot()
        g = snap["global"]

        total_processed = sum(e["events_processed"] for e in snap["by_route"].values())
        total_attempted = sum(
            e["deliveries_attempted"] for e in snap["by_route"].values()
        )
        total_succeeded = sum(
            e["deliveries_succeeded"] for e in snap["by_route"].values()
        )
        total_failed = sum(e["deliveries_failed"] for e in snap["by_route"].values())
        total_filter = sum(e["skipped_by_filter"] for e in snap["by_route"].values())
        total_loop = sum(e["skipped_by_loop"] for e in snap["by_route"].values())

        assert g["replay_events_processed"] == total_processed
        assert g["replay_deliveries_attempted"] == total_attempted
        assert g["replay_deliveries_succeeded"] == total_succeeded
        assert g["replay_deliveries_failed"] == total_failed
        assert g["replay_skipped_by_filter"] == total_filter
        assert g["replay_skipped_by_loop"] == total_loop

    def test_backlog_rejection_cancellation_in_global(self) -> None:
        rm = ReplayMetrics()
        rm.set_backlog_estimate(42)
        rm.record_rejection()
        rm.record_rejection()
        rm.record_cancellation()
        g = rm.snapshot()["global"]
        assert g["backlog_estimate"] == 42
        assert g["rejection_count"] == 2
        assert g["cancellation_count"] == 1
        assert g["last_cancelled_at"] is not None

    def test_backlog_clamped_to_zero(self) -> None:
        rm = ReplayMetrics()
        rm.set_backlog_estimate(-10)
        assert rm.snapshot()["global"]["backlog_estimate"] == 0

    def test_empty_replay_global_has_zeros(self) -> None:
        rm = ReplayMetrics()
        g = rm.snapshot()["global"]
        assert g["replay_events_processed"] == 0
        assert g["replay_deliveries_attempted"] == 0
        assert g["replay_deliveries_succeeded"] == 0
        assert g["replay_deliveries_failed"] == 0
        assert g["replay_skipped_by_filter"] == 0
        assert g["replay_skipped_by_loop"] == 0
        assert g["backlog_estimate"] == 0
        assert g["rejection_count"] == 0
        assert g["cancellation_count"] == 0
        assert g["last_cancelled_at"] is None


class TestCapacityConsistency:
    """CapacityController snapshot reflects internal state."""

    def test_initial_snapshot_all_zeros_except_limit_and_accepting(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        snap = ctrl.snapshot()
        assert snap["accepting_work"] is True
        assert snap["delivery_current"] == 0
        assert snap["delivery_limit"] == limits.max_inflight_deliveries
        assert snap["replay_current"] == 0
        assert snap["replay_limit"] == limits.max_inflight_replay_events
        assert snap["delivery_rejections"] == 0
        assert snap["delivery_timeouts"] == 0
        assert snap["replay_rejections"] == 0
        assert snap["replay_timeouts"] == 0

    def test_stop_accepting_reflected_in_snapshot(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        ctrl.stop_accepting()
        snap = ctrl.snapshot()
        assert snap["accepting_work"] is False

    def test_diagnostic_snapshot_includes_capacity_when_provided(self) -> None:
        rs = RouteStats()
        rm = ReplayMetrics()
        cap_data = {
            "accepting_work": True,
            "delivery_current": 5,
            "delivery_limit": 50,
            "delivery_rejections": 1,
            "delivery_timeouts": 0,
            "replay_current": 2,
            "replay_limit": 25,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }
        snap = build_diagnostics_snapshot(rs, rm, capacity_snapshot=cap_data)
        assert snap["capacity"] == cap_data
        # Also verify it round-trips through JSON
        json_str = json.dumps(snap, sort_keys=True)
        assert json.loads(json_str)["capacity"] == cap_data


# ===================================================================
# 6. Route-stat consistency
# ===================================================================


class TestRouteStatConsistency:
    """RouteStats counters are uniform and well-typed across all routes."""

    def test_delivered_increments_only_delivered(self) -> None:
        rs = RouteStats()
        rs.record_delivered("r")
        e = rs.snapshot()["r"]
        assert e["delivered"] == 1
        assert e["failed"] == 0
        assert e["skipped"] == 0
        assert e["loop_prevented"] == 0

    def test_failed_increments_only_failed(self) -> None:
        rs = RouteStats()
        rs.record_failed("r", "err")
        e = rs.snapshot()["r"]
        assert e["delivered"] == 0
        assert e["failed"] == 1
        assert e["skipped"] == 0
        assert e["loop_prevented"] == 0

    def test_skipped_increments_only_skipped(self) -> None:
        rs = RouteStats()
        rs.record_skipped("r")
        e = rs.snapshot()["r"]
        assert e["delivered"] == 0
        assert e["failed"] == 0
        assert e["skipped"] == 1
        assert e["loop_prevented"] == 0

    def test_loop_prevented_increments_only_loop_prevented(self) -> None:
        rs = RouteStats()
        rs.record_loop_prevented("r")
        e = rs.snapshot()["r"]
        assert e["delivered"] == 0
        assert e["failed"] == 0
        assert e["skipped"] == 0
        assert e["loop_prevented"] == 1

    def test_counters_accumulate(self) -> None:
        rs = RouteStats()
        for _ in range(5):
            rs.record_delivered("r")
        for _ in range(3):
            rs.record_failed("r", "e")
        assert rs.snapshot()["r"]["delivered"] == 5
        assert rs.snapshot()["r"]["failed"] == 3

    def test_last_error_updates_on_each_failure(self) -> None:
        rs = RouteStats()
        rs.record_failed("r", "first error")
        rs.record_failed("r", "second error")
        assert rs.snapshot()["r"]["last_error"] == "second error"

    def test_independent_routes_dont_interfere(self) -> None:
        rs = RouteStats()
        rs.record_delivered("r1")
        rs.record_failed("r2", "err")
        snap = rs.snapshot()
        assert snap["r1"]["delivered"] == 1
        assert snap["r1"]["failed"] == 0
        assert snap["r2"]["delivered"] == 0
        assert snap["r2"]["failed"] == 1
        assert "last_error" not in snap["r1"]
        assert "last_error" in snap["r2"]

    def test_route_entry_types_are_json_safe(self) -> None:
        rs = RouteStats()
        rs.record_delivered("r")
        rs.record_failed("r", "some error")
        entry = rs.snapshot()["r"]
        for key in _EXPECTED_ROUTE_ENTRY_KEYS:
            assert isinstance(entry[key], int)
        assert isinstance(entry.get("last_error", ""), str)


# ===================================================================
# 7. JSON-safe exports
# ===================================================================


def _assert_json_safe(obj: Any, path: str = "root") -> None:
    """Recursively assert that *obj* contains only JSON-safe leaf types."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"Non-string key at {path}: {k!r}"
            _assert_json_safe(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_json_safe(v, f"{path}[{i}]")
    elif isinstance(obj, bool | int | float | str | type(None)):
        pass  # Safe
    else:
        pytest.fail(f"Non-JSON-safe type at {path}: {type(obj).__name__} = {obj!r:.80}")


class TestJsonSafeExports:
    """Every snapshot export must survive json.dumps and contain only
    JSON-safe types."""

    def test_route_stats_snapshot_is_json_safe(self) -> None:
        rs = RouteStats()
        rs.record_delivered("r")
        rs.record_failed("r", "err with special chars: \x00\x01")
        snap = rs.snapshot()
        _assert_json_safe(snap)
        json.dumps(snap)

    def test_replay_metrics_snapshot_is_json_safe(self) -> None:
        rm = ReplayMetrics()
        rm.record_events_processed("r")
        rm.record_cancellation()
        snap = rm.snapshot()
        # last_cancelled_at is float|None — both JSON-safe
        _assert_json_safe(snap)
        json.dumps(snap)

    def test_diagnostics_snapshot_is_json_safe(self) -> None:
        rs = RouteStats()
        rm = ReplayMetrics()
        for rid in ["a", "b"]:
            rs.record_delivered(rid)
            rm.record_events_processed(rid)
        snap = build_diagnostics_snapshot(rs, rm)
        _assert_json_safe(snap)
        json.dumps(snap)

    def test_accounting_snapshot_is_json_safe(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_delivered()
        snap = acc.snapshot()
        _assert_json_safe(snap)
        json.dumps(snap)

    def test_capacity_snapshot_is_json_safe(self) -> None:
        limits = RuntimeLimits()
        ctrl = CapacityController(limits)
        snap = ctrl.snapshot()
        _assert_json_safe(snap)
        json.dumps(snap)

    def test_runtime_snapshot_is_json_safe(self) -> None:
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter()},
            route_stats=_FakeRouteStats(
                {"r1": {"delivered": 1, "failed": 0, "skipped": 0, "loop_prevented": 0}}
            ),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {}}),
            accounting=RuntimeAccounting(),
        )
        snap = build_runtime_snapshot(app)
        _assert_json_safe(snap)
        json.dumps(snap, sort_keys=True)

    def test_normalize_diagnostics_produces_json_safe_output(self) -> None:
        result = normalize_diagnostics(
            {
                "connected": True,
                "health": "healthy",
                "extra": [1, 2, 3],
            }
        )
        _assert_json_safe(result)
        json.dumps(result)

    def test_normalize_diagnostics_replaces_unsafe_types(self) -> None:
        """Exceptions and raw objects should be replaced with type-name strings."""
        result = normalize_diagnostics(
            {
                "connected": True,
                "custom_obj": ValueError("oops"),
            }
        )
        assert isinstance(result["transport_specific"]["custom_obj"], str)
        # Should be something like "<ValueError>"
        assert "ValueError" in result["transport_specific"]["custom_obj"]

    def test_runtime_snapshot_json_dumps_with_sort_keys(self) -> None:
        """Full round-trip: json.dumps(sort_keys=True) must not raise."""
        app = _make_fake_app(
            adapters={"z": _FakeAdapter("z"), "a": _FakeAdapter("a")},
            route_stats=_FakeRouteStats(
                {
                    "r2": {
                        "delivered": 0,
                        "failed": 1,
                        "skipped": 0,
                        "loop_prevented": 0,
                    },
                    "r1": {
                        "delivered": 5,
                        "failed": 0,
                        "skipped": 2,
                        "loop_prevented": 0,
                    },
                }
            ),
            capacity_controller=_FakeCapacityController(),
            accounting=RuntimeAccounting(),
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap, sort_keys=True)
        deserialized = json.loads(serialized)
        assert deserialized == snap


# ===================================================================
# 8. No secret leakage
# ===================================================================


class TestNoSecretLeakage:
    """Snapshots must not contain tokens, passwords, or raw SDK objects."""

    @pytest.mark.parametrize(
        "secret",
        [
            "syt_ABCdef123456",
            "MDAxYWJjZGVmZ2hpamtsbW5vcA==",
            "sk-0123456789abcdef0123456789",
            "api_key=super_secret_value",
            "access_token=tok_abc123",
            "password=hunter2",
            "secret=s3cr3t",
            "api_key: my_key_here",
        ],
    )
    def test_sanitize_error_redacts_known_patterns(self, secret: str) -> None:
        msg = f"Connection failed: {secret}"
        sanitized = _sanitize_error(msg)
        assert secret not in sanitized
        assert "[REDACTED]" in sanitized

    def test_sanitize_error_removes_sdk_repr(self) -> None:
        msg = "Got object <nio.client.AsyncClient object at 0x7f1234567890>"
        sanitized = _sanitize_error(msg)
        assert "<nio.client.AsyncClient object at 0x7f1234567890>" not in sanitized
        assert "[OBJECT_REPR]" in sanitized

    def test_sanitize_error_truncates_long_strings(self) -> None:
        long_msg = "x" * 600
        sanitized = _sanitize_error(long_msg)
        assert len(sanitized) <= _MAX_ERROR_DETAIL_LEN

    def test_route_stats_sanitizes_errors_in_snapshot(self) -> None:
        rs = RouteStats()
        rs.record_failed("r", "Error: syt_SECRET_TOKEN_12345 connection lost")
        snap = rs.snapshot()
        error = snap["r"]["last_error"]
        assert "syt_SECRET_TOKEN_12345" not in error

    @pytest.mark.parametrize(
        "secret_key",
        [
            "password",
            "secret",
            "private_key",
            "access_token",
            "auth_token",
            "api_key",
            "credential",
            "credentials",
            "session_secret",
            "encryption_key",
        ],
    )
    def test_normalize_diagnostics_drops_secret_keys(self, secret_key: str) -> None:
        result = normalize_diagnostics(
            {
                "connected": True,
                secret_key: "super_secret_value_12345",
            }
        )
        # The secret key should not appear at top level
        assert secret_key not in result
        # And not in transport_specific either
        if "transport_specific" in result:
            assert secret_key not in result["transport_specific"]

    def test_runtime_snapshot_never_includes_adapter_config(self) -> None:
        """Adapters with config attributes should not have them in snapshot."""
        adapter = _FakeAdapter("sensitive")
        # Manually add a config-like attribute
        adapter._config = {"access_token": "syt_should_not_appear"}  # type: ignore[attr-defined]
        app = _make_fake_app(adapters={"sensitive": adapter})
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap)
        assert "syt_should_not_appear" not in serialized
        assert "access_token" not in serialized

    def test_build_failure_errors_are_sanitized(self) -> None:
        bf = _FakeBuildFailure(
            adapter_id="leaky",
            error="Failed with token syt_SECRET123 and password=opensesame",
        )
        app = _make_fake_app(build_failures=[bf])
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap)
        assert "syt_SECRET123" not in serialized
        assert "opensesame" not in serialized

    def test_normalize_diagnostics_case_insensitive_secret_keys(self) -> None:
        """Secret detection should be case-insensitive."""
        result = normalize_diagnostics(
            {
                "connected": True,
                "Password": "secret_val",
                "API_KEY": "key_val",
            }
        )
        assert "Password" not in result
        assert "API_KEY" not in result

    def test_no_nan_or_infinity_in_snapshots(self) -> None:
        """NaN and Infinity are not JSON-safe and must not appear."""
        acc = RuntimeAccounting()
        snap = acc.snapshot()
        for key, val in snap.items():
            if isinstance(val, float):
                assert not math.isnan(val), f"{key} is NaN"
                assert not math.isinf(val), f"{key} is Infinity"
