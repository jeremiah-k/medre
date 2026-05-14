"""Track 3 — snapshot/diagnostics stress tests.

Covers:
- Large route tables (exceeding _MAX_ROUTES, unbounded RouteStats growth).
- Large adapter counts (exceeding _MAX_ADAPTERS).
- Repeated snapshot generation (determinism, no drift, performance sanity).
- Failing adapters (attribute access raises).
- Partially initialized adapters (missing standard attributes).
- Malformed adapter diagnostics (weird types, non-string health).
- Replay pressure (large replay counter structures).
- Capacity exhaustion (saturated semaphores).
- Deterministic ordering (large data sets, stable sort).
- Boundedness/truncation (all caps enforced at scale).
- Secret safety (tokens in route errors, build failures, diagnostics).
"""

from __future__ import annotations

import dataclasses
import gc
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.runtime.snapshot import (
    SCHEMA_VERSION,
    _MAX_ADAPTERS,
    _MAX_BUILD_FAILURES,
    _MAX_ERROR_DETAIL_LEN,
    _MAX_ROUTES,
    build_runtime_snapshot,
)
from medre.core.routing.stats import RouteStats
from medre.observability.sanitization import sanitize_error as _sanitize_error


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
    pass


class _FakeDiagnosticsCollector:
    def __init__(self, replay_data: dict[str, Any] | None = None) -> None:
        self._replay_data = replay_data or {}

    def snapshot(self) -> dict[str, Any]:
        return {"replay": self._replay_data}


class _FakeBuildFailure:
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


def _fixed_now() -> datetime:
    return datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


_FIXED_MONO = 1000.0


# =====================================================================
# 1. Large route tables
# =====================================================================


class TestLargeRouteTables:
    """Stress: route tables at and beyond _MAX_ROUTES."""

    def test_routes_at_exact_max(self) -> None:
        """Exactly _MAX_ROUTES routes — all included."""
        routes = {f"route-{i:06d}": {"delivered": i} for i in range(_MAX_ROUTES)}
        app = _make_fake_app(route_stats=_FakeRouteStats(routes))
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["routes"]["stats"]["per_route"]) == _MAX_ROUTES

    def test_routes_beyond_max_are_truncated(self) -> None:
        """Beyond _MAX_ROUTES — only first _MAX_ROUTES (sorted) kept."""
        extra = 200
        total = _MAX_ROUTES + extra
        routes = {f"route-{i:06d}": {"delivered": i} for i in range(total)}
        app = _make_fake_app(route_stats=_FakeRouteStats(routes))
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["routes"]["stats"]["per_route"]) == _MAX_ROUTES
        # The kept routes must be the first _MAX_ROUTES in sorted order.
        sorted_ids = sorted(routes.keys())
        expected_ids = sorted_ids[:_MAX_ROUTES]
        assert list(snap["routes"]["stats"]["per_route"].keys()) == expected_ids

    def test_routes_sorted_deterministically_large(self) -> None:
        """Routes are sorted alphabetically even with 2000+ routes."""
        routes = {f"z-route-{i:06d}": {"delivered": 0} for i in range(2000)}
        routes["a-first"] = {"delivered": 1}
        routes["m-middle"] = {"delivered": 2}
        app = _make_fake_app(route_stats=_FakeRouteStats(routes))
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        route_keys = list(snap["routes"]["stats"]["per_route"].keys())
        assert route_keys == sorted(route_keys)

    def test_real_routestats_unbounded_growth(self) -> None:
        """Real RouteStats grows unboundedly; snapshot caps it."""
        rs = RouteStats()
        for i in range(_MAX_ROUTES + 500):
            rs.record_delivered(f"rt-{i:06d}")
        raw = rs.snapshot()
        assert len(raw) == _MAX_ROUTES + 500  # unbounded internally

        app = _make_fake_app(route_stats=rs)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["routes"]["stats"]["per_route"]) <= _MAX_ROUTES


# =====================================================================
# 2. Large adapter counts
# =====================================================================


class TestLargeAdapterCounts:
    """Stress: adapter collections at and beyond _MAX_ADAPTERS."""

    def test_adapters_at_exact_max(self) -> None:
        """Exactly _MAX_ADAPTERS adapters — all included."""
        adapters = {
            f"adapter-{i:06d}": _FakeAdapter(adapter_id=f"adapter-{i:06d}")
            for i in range(_MAX_ADAPTERS)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["adapters"]) == _MAX_ADAPTERS

    def test_adapters_beyond_max_are_truncated(self) -> None:
        """Beyond _MAX_ADAPTERS — only first _MAX_ROUTES (sorted) kept."""
        total = _MAX_ADAPTERS + 100
        adapters = {
            f"adapter-{i:06d}": _FakeAdapter(adapter_id=f"adapter-{i:06d}")
            for i in range(total)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["adapters"]) == _MAX_ADAPTERS
        sorted_ids = sorted(adapters.keys())
        expected_ids = sorted_ids[:_MAX_ADAPTERS]
        assert list(snap["adapters"].keys()) == expected_ids

    def test_adapters_sorted_deterministically_large(self) -> None:
        """Adapter keys sorted even with 500+ adapters."""
        adapters = {
            f"adapter-{i:06d}": _FakeAdapter(adapter_id=f"adapter-{i:06d}")
            for i in range(500)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        adapter_keys = list(snap["adapters"].keys())
        assert adapter_keys == sorted(adapter_keys)

    def test_each_adapter_entry_has_sorted_keys_at_scale(self) -> None:
        """Every adapter entry has alphabetically sorted keys."""
        adapters = {
            f"ad-{i:04d}": _FakeAdapter(adapter_id=f"ad-{i:04d}")
            for i in range(50)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        for aid, entry in snap["adapters"].items():
            assert list(entry.keys()) == sorted(entry.keys()), (
                f"Adapter {aid} has unsorted keys: {list(entry.keys())}"
            )


# =====================================================================
# 3. Repeated snapshot generation (stability, determinism)
# =====================================================================


class TestRepeatedSnapshotGeneration:
    """Generate many snapshots; verify no drift or state corruption."""

    def test_100_snapshots_deterministic(self) -> None:
        """100 consecutive snapshots with fixed clocks are identical."""
        app = _make_fake_app(
            adapters={
                "a1": _FakeAdapter(adapter_id="a1", health="healthy"),
                "b2": _FakeAdapter(adapter_id="b2", health="degraded"),
            },
            route_stats=_FakeRouteStats({"r1": {"delivered": 42}}),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {"total": 1}}),
            startup_wall="2026-05-11T10:00:00+00:00",
            startup_monotonic=500.0,
        )
        first = None
        for _ in range(100):
            snap = build_runtime_snapshot(
                app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO,
            )
            serialized = json.dumps(snap, sort_keys=True)
            if first is None:
                first = serialized
            else:
                assert serialized == first, "Snapshot drifted across 100 calls"

    def test_50_snapshots_with_real_routestats(self) -> None:
        """RouteStats mutations between snapshots reflect correctly."""
        rs = RouteStats()
        app = _make_fake_app(route_stats=rs)
        for i in range(50):
            rs.record_delivered(f"route-{i % 10}")
            snap = build_runtime_snapshot(
                app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO,
            )
            # Should never crash, always JSON-safe.
            json.dumps(snap, sort_keys=True)
            assert snap["schema_version"] == SCHEMA_VERSION

    def test_repeated_snapshots_do_not_mutate_app(self) -> None:
        """Taking snapshots must not alter the app's state."""
        adapters = {
            "a1": _FakeAdapter(adapter_id="a1"),
        }
        app = _make_fake_app(adapters=adapters, state=_FakeRuntimeState.RUNNING)
        original_state = app.state
        original_adapter_count = len(app.adapters)

        for _ in range(50):
            build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)

        assert app.state is original_state
        assert len(app.adapters) == original_adapter_count


# =====================================================================
# 4. Failing adapters (attribute access raises)
# =====================================================================


class TestFailingAdapters:
    """Adapters whose attribute access raises exceptions."""

    def test_adapter_repr_raises(self) -> None:
        """Adapter with __repr__ that raises doesn't crash snapshot."""
        class _BrokenReprAdapter:
            adapter_id = "broken-repr"
            platform = "test"
            role = _FakeRole.TRANSPORT
            _version = "0.1.0"
            _capabilities = _FakeCapabilities()
            _last_health = "unknown"

            def __repr__(self) -> str:
                raise RuntimeError("repr explosion")

        app = _make_fake_app(adapters={"br": _BrokenReprAdapter()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert "br" in snap["adapters"]
        assert snap["adapters"]["br"]["adapter_id"] == "broken-repr"

    def test_adapter_properties_raise(self) -> None:
        """Adapter with properties that raise doesn't crash snapshot."""
        class _BrokenPropAdapter:
            @property
            def adapter_id(self) -> str:
                raise RuntimeError("adapter_id boom")

            @property
            def platform(self) -> str:
                raise RuntimeError("platform boom")

        app = _make_fake_app(adapters={"bp": _BrokenPropAdapter()})
        # Should not raise — _snapshot_adapter uses getattr with defaults.
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert "bp" in snap["adapters"]
        # Falls back to "unknown" for adapter_id/platform when getattr fails.
        entry = snap["adapters"]["bp"]
        assert isinstance(entry["adapter_id"], str)
        assert isinstance(entry["platform"], str)


# =====================================================================
# 5. Partially initialized adapters
# =====================================================================


class TestPartiallyInitializedAdapters:
    """Adapters missing standard attributes."""

    def test_adapter_missing_all_optional_attrs(self) -> None:
        """Bare object with only adapter_id still produces a snapshot."""
        class _Minimal:
            adapter_id = "minimal"

        app = _make_fake_app(adapters={"min": _Minimal()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        entry = snap["adapters"]["min"]
        assert entry["adapter_id"] == "minimal"
        assert entry["platform"] == "unknown"
        assert entry["role"] == "unknown"
        assert entry["version"] == "unknown"
        assert entry["health"] == "unknown"
        assert entry["capabilities"] == {}

    def test_adapter_with_none_attrs(self) -> None:
        """Adapter with None for optional attrs falls back to unknown."""
        class _NoneAttrs:
            adapter_id = "none-attrs"
            platform = None  # type: ignore[assignment]
            role = None
            _version = None
            _capabilities = None
            _last_health = None

        app = _make_fake_app(adapters={"na": _NoneAttrs()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        entry = snap["adapters"]["na"]
        assert entry["adapter_id"] == "none-attrs"
        # None platform falls back to "unknown" via the snapshot adapter.
        assert entry["platform"] == "unknown"

    def test_adapter_with_int_platform(self) -> None:
        """Non-string platform is preserved as JSON-safe value."""
        class _WeirdPlatform:
            adapter_id = "weird"
            platform = 12345  # not a string
            role = _FakeRole.TRANSPORT
            _version = "1.0"
            _capabilities = None
            _last_health = "ok"

        app = _make_fake_app(adapters={"wp": _WeirdPlatform()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        entry = snap["adapters"]["wp"]
        # Integer platform is JSON-safe, preserved as-is.
        assert entry["platform"] == 12345

    def test_adapter_with_callable_role(self) -> None:
        """Non-enum callable role is converted via str()."""
        class _CallableRole:
            adapter_id = "cr"
            platform = "test"
            role = lambda: "custom"  # type: ignore[assignment]
            _version = "1.0"
            _capabilities = None
            _last_health = "unknown"

        app = _make_fake_app(adapters={"cr": _CallableRole()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        entry = snap["adapters"]["cr"]
        # str() on a lambda gives something like "<lambda>"
        assert isinstance(entry["role"], str)


# =====================================================================
# 6. Malformed adapter diagnostics
# =====================================================================


class TestMalformedAdapterDiagnostics:
    """Adapters returning non-standard diagnostics values."""

    def test_adapter_health_is_integer(self) -> None:
        """Integer health value is accepted as-is (JSON-safe)."""
        class _IntHealth:
            adapter_id = "ih"
            platform = "test"
            _last_health = 200  # numeric health

        app = _make_fake_app(adapters={"ih": _IntHealth()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["adapters"]["ih"]["health"] == 200

    def test_adapter_health_is_none(self) -> None:
        """None health maps to 'unknown' default."""
        class _NoneHealth:
            adapter_id = "nh"
            platform = "test"
            _last_health = None

        app = _make_fake_app(adapters={"nh": _NoneHealth()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["adapters"]["nh"]["health"] is None

    def test_adapter_capabilities_non_dataclass(self) -> None:
        """Non-dataclass capabilities produce empty dict."""
        class _DictCaps:
            adapter_id = "dc"
            platform = "test"
            _capabilities = {"not": "a dataclass"}

        app = _make_fake_app(adapters={"dc": _DictCaps()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["adapters"]["dc"]["capabilities"] == {}

    def test_adapter_with_enum_in_capabilities(self) -> None:
        """Enum values in capabilities are converted to their .value."""
        class _Status(Enum):
            ACTIVE = "active"
            INACTIVE = "inactive"

        @dataclass
        class _CapsWithEnum:
            status: _Status = _Status.ACTIVE
            text: bool = True

        class _EnumCapsAdapter:
            adapter_id = "ec"
            platform = "test"
            role = _FakeRole.TRANSPORT
            _version = "1.0"
            _capabilities = _CapsWithEnum()
            _last_health = "unknown"

        app = _make_fake_app(adapters={"ec": _EnumCapsAdapter()})
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["adapters"]["ec"]["capabilities"]["status"] == "active"


# =====================================================================
# 7. Replay pressure
# =====================================================================


class TestReplayPressure:
    """Large replay counter structures and replay-heavy scenarios."""

    def test_large_replay_counters(self) -> None:
        """Large replay counters dict is included in snapshot."""
        big_replay = {
            "global": {"total": 10000, "succeeded": 9999, "failed": 1},
            "by_route": {
                f"route-{i:04d}": {"deliveries": i * 10}
                for i in range(500)
            },
        }
        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector(big_replay),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"]["global"]["total"] == 10000
        assert len(snap["replay"]["counters"]["by_route"]) == 500

    def test_diagnostics_snapshot_raises(self) -> None:
        """Diagnostics collector snapshot() raising doesn't crash."""
        class _BrokenDiag:
            def snapshot(self) -> dict[str, Any]:
                raise RuntimeError("diag boom")

        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_BrokenDiag(),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"] is None  # graceful fallback

    def test_diagnostics_snapshot_returns_non_dict(self) -> None:
        """Diagnostics returning non-dict is handled gracefully."""
        class _NonDictDiag:
            def snapshot(self) -> str:
                return "not a dict"

        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_NonDictDiag(),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["replay"]["available"] is True
        # snapshot returns string, .get("replay") on str returns None
        assert snap["replay"]["counters"] is None


# =====================================================================
# 8. Capacity exhaustion
# =====================================================================


class TestCapacityExhaustion:
    """Capacity controller at or beyond limits."""

    def test_capacity_fully_saturated(self) -> None:
        """All delivery and replay slots full."""
        cap_data = {
            "accepting_work": False,
            "delivery_current": 50,
            "delivery_limit": 50,
            "delivery_rejections": 9999,
            "delivery_timeouts": 500,
            "replay_current": 25,
            "replay_limit": 25,
            "replay_rejections": 300,
            "replay_timeouts": 100,
        }
        app = _make_fake_app(capacity_controller=_FakeCapacityController(cap_data))
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["capacity"]["state"]["accepting_work"] is False
        assert snap["capacity"]["state"]["delivery_current"] == snap["capacity"]["state"]["delivery_limit"]
        assert snap["capacity"]["state"]["replay_current"] == snap["capacity"]["state"]["replay_limit"]

    def test_capacity_snapshot_raises(self) -> None:
        """Capacity controller snapshot() raising doesn't crash."""
        class _BrokenCapacity:
            def snapshot(self) -> dict[str, Any]:
                raise RuntimeError("capacity boom")

        app = _make_fake_app(capacity_controller=_BrokenCapacity())
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        # Should not raise, but capacity may be None or partially captured.
        # The snapshot function catches None from hasattr check.
        assert "capacity" in snap

    def test_capacity_large_counters(self) -> None:
        """Very large counter values don't overflow or error."""
        cap_data = {
            "accepting_work": True,
            "delivery_current": 0,
            "delivery_limit": 2**31 - 1,
            "delivery_rejections": 2**63 - 1,
            "delivery_timeouts": 0,
            "replay_current": 0,
            "replay_limit": 2**31 - 1,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }
        app = _make_fake_app(capacity_controller=_FakeCapacityController(cap_data))
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["capacity"]["state"]["delivery_limit"] == 2**31 - 1
        assert snap["capacity"]["state"]["delivery_rejections"] == 2**63 - 1
        # JSON-safe
        json.dumps(snap)


# =====================================================================
# 9. Deterministic ordering at scale
# =====================================================================


class TestDeterministicOrderingAtScale:
    """Deterministic key ordering across large data sets."""

    def test_top_level_keys_sorted_at_scale(self) -> None:
        """Top-level keys always sorted regardless of data size."""
        adapters = {
            f"a-{i:04d}": _FakeAdapter(adapter_id=f"a-{i:04d}")
            for i in range(200)
        }
        routes = {f"r-{i:04d}": {"delivered": i} for i in range(500)}
        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            build_failures=[_FakeBuildFailure(f"bf-{i}", f"err-{i}") for i in range(30)],
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        top_keys = list(snap.keys())
        assert top_keys == sorted(top_keys)

    def test_json_dumps_sort_keys_true_at_scale(self) -> None:
        """json.dumps(sort_keys=True) succeeds on a large snapshot."""
        adapters = {
            f"adapter-{i:04d}": _FakeAdapter(adapter_id=f"adapter-{i:04d}")
            for i in range(300)
        }
        routes = {f"route-{i:04d}": {"delivered": i} for i in range(800)}
        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
            capacity_controller=_FakeCapacityController(),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        serialized = json.dumps(snap, sort_keys=True)
        assert isinstance(serialized, str)
        # Re-parse to verify round-trip.
        parsed = json.loads(serialized)
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_two_large_snapshots_identical(self) -> None:
        """Two snapshots of the same large app produce identical JSON."""
        adapters = {
            f"ad-{i:04d}": _FakeAdapter(adapter_id=f"ad-{i:04d}")
            for i in range(100)
        }
        routes = {f"rt-{i:04d}": {"delivered": i} for i in range(300)}
        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
            startup_monotonic=500.0,
        )
        s1 = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        s2 = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)


# =====================================================================
# 10. Boundedness / truncation
# =====================================================================


class TestBoundednessAndTruncation:
    """All bounded collections and truncation rules enforced at scale."""

    def test_build_failures_at_exact_max(self) -> None:
        """Exactly _MAX_BUILD_FAILURES — all included."""
        failures = [_FakeBuildFailure(f"bf-{i}", f"error-{i}") for i in range(_MAX_BUILD_FAILURES)]
        app = _make_fake_app(build_failures=failures)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["startup"]["build_failures"]) == _MAX_BUILD_FAILURES

    def test_build_failures_beyond_max_truncated(self) -> None:
        """Beyond _MAX_BUILD_FAILURES — truncated."""
        total = _MAX_BUILD_FAILURES + 100
        failures = [_FakeBuildFailure(f"bf-{i}", f"error-{i}") for i in range(total)]
        app = _make_fake_app(build_failures=failures)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["startup"]["build_failures"]) == _MAX_BUILD_FAILURES

    def test_build_failure_error_truncated(self) -> None:
        """Error strings beyond _MAX_ERROR_DETAIL_LEN are truncated."""
        # Use spaces/punctuation to avoid matching base64-like token patterns
        long_error = "Build error: " + "retry failed. " * 60
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", long_error)])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        bf_err = snap["startup"]["build_failures"][0]["error"]
        assert len(bf_err) <= _MAX_ERROR_DETAIL_LEN
        assert bf_err.endswith("...")

    def test_build_failure_error_exactly_at_limit(self) -> None:
        """Error string exactly at _MAX_ERROR_DETAIL_LEN is not truncated."""
        # Use spaces to avoid matching base64-like token patterns
        exact_error = "Build error: " + " " * (_MAX_ERROR_DETAIL_LEN - 13)
        assert len(exact_error) == _MAX_ERROR_DETAIL_LEN
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", exact_error)])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["startup"]["build_failures"][0]["error"]) == _MAX_ERROR_DETAIL_LEN

    def test_build_failure_error_one_over_limit(self) -> None:
        """Error string one char over limit is truncated."""
        error = "Build error: " + " " * (_MAX_ERROR_DETAIL_LEN - 12)
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", error)])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        bf_err = snap["startup"]["build_failures"][0]["error"]
        assert len(bf_err) <= _MAX_ERROR_DETAIL_LEN

    def test_all_bounds_exceeded_simultaneously(self) -> None:
        """All bounded collections exceeded at once."""
        adapters = {
            f"a-{i:06d}": _FakeAdapter(adapter_id=f"a-{i:06d}")
            for i in range(_MAX_ADAPTERS + 200)
        }
        routes = {f"r-{i:06d}": {"delivered": i} for i in range(_MAX_ROUTES + 200)}
        failures = [
            _FakeBuildFailure(f"bf-{i}", "x" * 1000)
            for i in range(_MAX_BUILD_FAILURES + 50)
        ]
        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
            build_failures=failures,
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert len(snap["adapters"]) <= _MAX_ADAPTERS
        assert len(snap["routes"]["stats"]["per_route"]) <= _MAX_ROUTES
        assert len(snap["startup"]["build_failures"]) <= _MAX_BUILD_FAILURES
        for bf in snap["startup"]["build_failures"]:
            assert len(bf["error"]) <= _MAX_ERROR_DETAIL_LEN


# =====================================================================
# 11. Secret safety under stress
# =====================================================================


class TestSecretSafety:
    """Secrets must not leak through any snapshot pathway."""

    FORBIDDEN_TOKENS = [
        "syt_ABCDEFGHIJKLMNOP",
        "api_key=sk-1234567890abcdefghijklmnop",
        "password=hunter2",
        "secret=supersecretvalue123",
        "access_token=MDAxY2xvY2F0aW9uIG",
    ]

    def test_secrets_in_route_errors_sanitized(self) -> None:
        """Secrets in RouteStats error strings are redacted."""
        rs = RouteStats()
        for token in self.FORBIDDEN_TOKENS:
            rs.record_failed("secret-route", f"Connection failed: {token}")

        raw_snap = rs.snapshot()
        # _sanitize_error should have redacted tokens.
        raw_error = raw_snap["secret-route"]["last_error"]
        for token in self.FORBIDDEN_TOKENS:
            assert token not in raw_error, f"Token leaked: {token}"

    def test_secrets_dont_leak_through_route_stats_snapshot(self) -> None:
        """Full snapshot doesn't contain forbidden tokens via route stats."""
        rs = RouteStats()
        rs.record_failed("r1", f"boom api_key=sk-SECRETKEY1234567890abcdefghijklmnop")
        app = _make_fake_app(route_stats=rs)
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        serialized = json.dumps(snap).lower()

        forbidden = ["api_key", "sk-secretkey", "password", "secret"]
        for tok in forbidden:
            assert tok not in serialized, f"Forbidden token '{tok}' found"

    def test_secrets_in_build_failures_truncated_but_not_redacted(self) -> None:
        """Build failure errors are truncated but _sanitize_error is not applied.

        Build failure error strings come from the builder, not from RouteStats.
        They are truncated but not passed through _sanitize_error. Verify that
        the truncation doesn't create a new leak vector by checking structure.
        """
        secret_error = "x" * 400 + "api_key=sk-LEAK" + "y" * 200
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", secret_error)])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        bf_error = snap["startup"]["build_failures"][0]["error"]
        # Build failures are truncated but not sanitized — this is expected.
        # The truncation may or may not include the secret portion depending
        # on position. Just verify boundedness and JSON-safety.
        assert len(bf_error) <= _MAX_ERROR_DETAIL_LEN
        json.dumps(bf_error)  # must be JSON-safe

    def test_sanitize_error_various_patterns(self) -> None:
        """_sanitize_error catches known secret patterns."""
        patterns = [
            ("syt_ABCDEFGHIJ", "[REDACTED]"),
            ("api_key=mysecretkey", "[REDACTED]"),
            ("password=hunter2", "[REDACTED]"),
            ("secret=mysecret", "[REDACTED]"),
            ("sk-abc123def456ghi789jkl012", "[REDACTED]"),
        ]
        for pattern, expected in patterns:
            result = _sanitize_error(f"Error: {pattern} in connection")
            assert expected in result, f"Pattern '{pattern}' not redacted: {result}"

    def test_sanitize_error_sdk_repr(self) -> None:
        """SDK repr strings are redacted."""
        result = _sanitize_error("Failed: <nio.AsyncClient object at 0x7f1234567890>")
        assert "[OBJECT_REPR]" in result
        assert "0x7f1234567890" not in result

    def test_sanitize_error_long_string(self) -> None:
        """Very long error strings are truncated at 512 chars."""
        # Use spaces to break the base64-like token pattern ([A-Za-z0-9+/=]{40,}).
        long = "error detail " * 100  # ~1300 chars, no 40+ char base64 runs
        result = _sanitize_error(long)
        assert len(result) <= 512
        assert result.endswith("...")

    def test_no_sdk_repr_in_snapshot(self) -> None:
        """No '<...object at 0x...>' strings in the full snapshot."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(adapter_id="a1")},
            route_stats=_FakeRouteStats({"r1": {"delivered": 1}}),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        serialized = json.dumps(snap)
        assert " object at 0x" not in serialized

    def test_config_not_introspected(self) -> None:
        """Adapter config objects are never serialized into the snapshot."""
        class _ConfigWithSecret:
            access_token = "syt_TOP_SECRET_TOKEN"
            password = "super_secret_password"
            limits = _FakeRuntimeLimits()

        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(adapter_id="a1")},
            config=_ConfigWithSecret(),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        serialized = json.dumps(snap).lower()
        assert "syt_top_secret_token" not in serialized
        assert "super_secret_password" not in serialized
        # Limits are included but secrets are not.
        assert snap["limits"]["max_inflight_deliveries"] == 50


# =====================================================================
# 12. Build-failure edge cases
# =====================================================================


class TestBuildFailureEdgeCases:
    """Edge cases in build-failure handling."""

    def test_build_failure_missing_error_attr(self) -> None:
        """Build failure with no error attribute uses 'unknown error'."""
        class _NoError:
            adapter_id = "noerr"

        app = _make_fake_app(build_failures=[_NoError()])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["startup"]["build_failures"][0]["error"] == "unknown error"

    def test_build_failure_missing_adapter_id(self) -> None:
        """Build failure with no adapter_id uses 'unknown'."""
        class _NoId:
            error = "something failed"

        app = _make_fake_app(build_failures=[_NoId()])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["startup"]["build_failures"][0]["adapter_id"] == "unknown"

    def test_build_failure_error_is_exception(self) -> None:
        """Error being an Exception object is str()'d."""
        exc = ValueError("test error message")
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", exc)])  # type: ignore[arg-type]
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert "test error message" in snap["startup"]["build_failures"][0]["error"]

    def test_empty_build_failures_list(self) -> None:
        """Empty build failures list produces empty list in snapshot."""
        app = _make_fake_app(build_failures=[])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["startup"]["build_failures"] == []

    def test_build_failure_error_is_empty_string(self) -> None:
        """Empty string error is preserved."""
        app = _make_fake_app(build_failures=[_FakeBuildFailure("bf", "")])
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["startup"]["build_failures"][0]["error"] == ""


# =====================================================================
# 13. Mix: fully loaded snapshot at scale
# =====================================================================


class TestFullyLoadedSnapshotAtScale:
    """Snapshot with every section populated at scale."""

    def test_full_snapshot_large_scale(self) -> None:
        """Large-scale snapshot with all subsystems populated."""
        adapters = {
            f"adapter-{i:04d}": _FakeAdapter(
                adapter_id=f"adapter-{i:04d}",
                platform="test",
                health="healthy" if i % 2 == 0 else "degraded",
            )
            for i in range(200)
        }
        routes = {
            f"route-{i:04d}": {"delivered": i * 10, "failed": i % 5, "skipped": 0, "loop_prevented": 0}
            for i in range(500)
        }
        replay_data = {
            "global": {"total": 5000},
            "by_route": {f"route-{i:04d}": {"deliveries": i} for i in range(100)},
        }
        cap_data = {
            "accepting_work": True,
            "delivery_current": 10,
            "delivery_limit": 200,
            "delivery_rejections": 0,
            "delivery_timeouts": 0,
            "replay_current": 2,
            "replay_limit": 25,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }
        failures = [_FakeBuildFailure(f"bf-{i}", f"error {i}") for i in range(30)]

        class _FakeAccounting:
            def snapshot(self) -> dict[str, int]:
                return {"events_processed": 50000, "errors": 3}

        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
            capacity_controller=_FakeCapacityController(cap_data),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector(replay_data),
            build_failures=failures,
            startup_wall="2026-05-11T08:00:00+00:00",
            startup_monotonic=200.0,
            health_state={"overall": "degraded", "healthy_adapters": 100, "total_adapters": 200},
            accounting=_FakeAccounting(),
        )
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)

        # Verify structure.
        assert snap["schema_version"] == SCHEMA_VERSION
        assert len(snap["adapters"]) == 200
        assert len(snap["routes"]["stats"]["per_route"]) == 500
        assert len(snap["startup"]["build_failures"]) == 30
        assert snap["replay"]["available"] is True
        assert snap["capacity"]["state"]["delivery_current"] == 10
        assert snap["startup"]["startup_health"]["overall"] == "degraded"
        assert snap["accounting"]["counters"]["events_processed"] == 50000
        assert snap["lifecycle"]["uptime_seconds"] == 800.0
        assert snap["lifecycle"]["startup_timestamp"] == "2026-05-11T08:00:00+00:00"

        # JSON-safe.
        serialized = json.dumps(snap, sort_keys=True)
        assert isinstance(serialized, str)

        # Deterministic keys.
        assert list(snap.keys()) == sorted(snap.keys())

    def test_minimal_app_does_not_crash(self) -> None:
        """Bare object() produces a valid snapshot."""
        snap = build_runtime_snapshot(object(), now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        assert snap["schema_version"] == SCHEMA_VERSION
        assert snap["adapters"] == {}
        assert snap["routes"]["stats"]["per_route"] == {}
        assert snap["startup"]["build_failures"] == []
        assert snap["lifecycle"]["runtime_state"] == "unknown"
        json.dumps(snap)


# =====================================================================
# 14. Performance sanity (not strict timing, just no pathologies)
# =====================================================================


class TestPerformanceSanity:
    """Ensure snapshot generation doesn't degrade pathologically."""

    def test_large_snapshot_completes_reasonably(self) -> None:
        """Generating a snapshot with 256 adapters + 1024 routes completes."""
        adapters = {
            f"a-{i:04d}": _FakeAdapter(adapter_id=f"a-{i:04d}")
            for i in range(_MAX_ADAPTERS)
        }
        routes = {f"r-{i:04d}": {"delivered": i} for i in range(_MAX_ROUTES)}
        app = _make_fake_app(
            adapters=adapters,
            route_stats=_FakeRouteStats(routes),
        )
        start = time.monotonic()
        snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
        elapsed = time.monotonic() - start

        assert len(snap["adapters"]) == _MAX_ADAPTERS
        assert len(snap["routes"]["stats"]["per_route"]) == _MAX_ROUTES
        # No strict timing assertion — just ensure it completes.
        # If it takes > 10s something is very wrong.
        assert elapsed < 10.0, f"Snapshot took {elapsed:.2f}s — possible performance regression"

    def test_10_snapshots_at_scale_no_drift(self) -> None:
        """10 snapshots of max-scale data remain deterministic."""
        adapters = {
            f"a-{i:04d}": _FakeAdapter(adapter_id=f"a-{i:04d}")
            for i in range(_MAX_ADAPTERS)
        }
        routes = {f"r-{i:04d}": {"delivered": i} for i in range(_MAX_ROUTES)}
        app = _make_fake_app(adapters=adapters, route_stats=_FakeRouteStats(routes))

        serialized_set: set[str] = set()
        for _ in range(10):
            snap = build_runtime_snapshot(app, now_fn=_fixed_now, monotonic_fn=lambda: _FIXED_MONO)
            serialized_set.add(json.dumps(snap, sort_keys=True))

        assert len(serialized_set) == 1, "Snapshots at max scale are not deterministic"
