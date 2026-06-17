"""Tests for the runtime snapshot module.

Covers:
- Deterministic output (same inputs -> same snapshot, sorted keys).
- JSON safety (``json.dumps`` succeeds, no SDK objects).
- Sanitisation / no secrets (no tokens, keys, or raw SDK objects).
- Bounded size (adapter/route collections are capped).
- Representative snapshot contents (adapters, routes, capacity, limits, replay, state).
- Graceful handling of absent optional structures.
- Build-failure inclusion.
- Startup timestamp and uptime computation.
- Schema version presence.
- Startup health state tolerance (null when absent, dict when present).
- Live health null before refresh, populated after refresh_live_health() (scope transitions "startup" to "live").
- Sectioned schema (schema_version 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.runtime.snapshot import (
    _MAX_ADAPTERS,
    _MAX_BUILD_FAILURES,
    _MAX_ERROR_DETAIL_LEN,
    _MAX_ROUTES,
    SCHEMA_VERSION,
    build_runtime_snapshot,
)
from tests.helpers.snapshot import (
    FakeAdapter,
    FakeBuildFailure,
    FakeCapabilities,
    FakeCapacityController,
    FakeDiagnosticsCollector,
    FakeReplayEngine,
    FakeRole,
    FakeRouteStats,
    FakeRuntimeConfig,
    FakeRuntimeLimits,
    FakeRuntimeState,
    make_fake_app,
)

# ---------------------------------------------------------------------------
# Backward-compatible aliases — local tests still use the underscore-prefixed
# names.  These simply point at the helpers module versions.
# ---------------------------------------------------------------------------
_UNSET = object()  # sentinel kept for local _make_fake_app wrapper


# Alias the helper module names so existing tests don't need rewriting.
_FakeRole = FakeRole
_FakeCapabilities = FakeCapabilities
_FakeAdapter = FakeAdapter
_FakeRuntimeLimits = FakeRuntimeLimits
_FakeRuntimeConfig = FakeRuntimeConfig
_FakeRouteStats = FakeRouteStats
_FakeCapacityController = FakeCapacityController
_FakeReplayEngine = FakeReplayEngine
_FakeDiagnosticsCollector = FakeDiagnosticsCollector
_FakeBuildFailure = FakeBuildFailure
_FakeRuntimeState = FakeRuntimeState


# Local wrapper that preserves the old underscore-prefixed API used
# throughout this test module.  The real logic lives in helpers/snapshot.py.
def _make_fake_app(**kwargs: Any) -> Any:
    return make_fake_app(**kwargs)


# ---------------------------------------------------------------------------
# Tests: Deterministic ordering
# ---------------------------------------------------------------------------
class TestDeterministicOrdering:
    """Snapshot output must be deterministic across identical calls."""

    def test_two_identical_calls_produce_same_json(self) -> None:
        """Two snapshots with the same inputs produce identical JSON strings."""
        fixed_now = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
        fixed_mono = 1000.0

        app = _make_fake_app(
            adapters={
                "beta-adapter": _FakeAdapter(adapter_id="beta-adapter"),
                "alpha-adapter": _FakeAdapter(adapter_id="alpha-adapter"),
            },
        )

        snap1 = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_now,
            monotonic_fn=lambda: fixed_mono,
        )
        snap2 = build_runtime_snapshot(
            app,
            now_fn=lambda: fixed_now,
            monotonic_fn=lambda: fixed_mono,
        )

        assert json.dumps(snap1, sort_keys=True) == json.dumps(snap2, sort_keys=True)

    def test_top_level_keys_are_sorted(self) -> None:
        """Top-level dict keys must be in alphabetical order."""
        snap = build_runtime_snapshot(_make_fake_app())
        keys = list(snap.keys())
        assert keys == sorted(keys)

    def test_adapter_keys_are_sorted(self) -> None:
        """Adapters sub-dict keys must be sorted alphabetically."""
        app = _make_fake_app(
            adapters={
                "zebra": _FakeAdapter(adapter_id="zebra"),
                "alpha": _FakeAdapter(adapter_id="alpha"),
                "middle": _FakeAdapter(adapter_id="middle"),
            },
        )
        snap = build_runtime_snapshot(app)
        adapter_keys = list(snap["adapters"].keys())
        assert adapter_keys == ["alpha", "middle", "zebra"]

    def test_adapter_internal_keys_are_sorted(self) -> None:
        """Each adapter entry's internal keys are sorted."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(adapter_id="a1")},
        )
        snap = build_runtime_snapshot(app)
        adapter_entry = snap["adapters"]["a1"]
        assert list(adapter_entry.keys()) == sorted(adapter_entry.keys())

    def test_limits_keys_are_sorted(self) -> None:
        """Limits sub-dict keys must be sorted."""
        snap = build_runtime_snapshot(_make_fake_app())
        limits = snap["limits"]
        assert list(limits.keys()) == sorted(limits.keys())

    def test_section_keys_are_sorted(self) -> None:
        """Section sub-dict keys (lifecycle, routes, etc.) must be sorted."""
        snap = build_runtime_snapshot(_make_fake_app())
        for section_name in (
            "lifecycle",
            "routes",
            "startup",
            "health",
            "diagnostics",
            "replay",
        ):
            section = snap[section_name]
            assert list(section.keys()) == sorted(
                section.keys()
            ), f"Section {section_name!r} keys not sorted"


# ---------------------------------------------------------------------------
# Tests: JSON safety
# ---------------------------------------------------------------------------
class TestJsonSafety:
    """Snapshot must be serialisable with json.dumps and contain no SDK objects."""

    def test_json_dumps_succeeds(self) -> None:
        """Full snapshot serialises without error."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter()},
            route_stats=_FakeRouteStats({"r1": {"delivered": 5}}),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {"total": 1}}),
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap, sort_keys=True)
        assert isinstance(serialized, str)

    def test_no_sdk_objects_in_values(self) -> None:
        """No value in the snapshot is an SDK object (non-JSON-native type)."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter()},
        )
        snap = build_runtime_snapshot(app)

        def _check_json_native(obj: Any, path: str = "root") -> None:
            if obj is None:
                return
            if isinstance(obj, (bool, int, float, str)):
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _check_json_native(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _check_json_native(v, f"{path}[{i}]")
            else:
                pytest.fail(f"Non-JSON-native value at {path}: {type(obj).__name__}")

        _check_json_native(snap)

    def test_no_enum_objects_leak(self) -> None:
        """Enum values must be converted to plain strings."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(role=_FakeRole.HYBRID)},
        )
        snap = build_runtime_snapshot(app)
        role_val = snap["adapters"]["a1"]["role"]
        assert isinstance(role_val, str)
        assert role_val == "hybrid"


# ---------------------------------------------------------------------------
# Tests: Sanitisation / no secrets
# ---------------------------------------------------------------------------
class TestSanitisation:
    """Snapshot must not contain secrets or raw SDK object representations."""

    def test_no_tokens_in_snapshot(self) -> None:
        """Token-like strings must not appear in the snapshot."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(adapter_id="a1")},
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap).lower()

        forbidden = ["syt_", "api_key", "password", "secret", "access_token"]
        for token in forbidden:
            assert (
                token not in serialized
            ), f"Forbidden token '{token}' found in snapshot"

    def test_no_sdk_repr_strings(self) -> None:
        """No '<module.Class object at 0x...>' strings in snapshot."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter()},
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap)
        assert " object at 0x" not in serialized

    def test_build_failure_errors_truncated(self) -> None:
        """Build failure error strings are truncated when too long."""
        long_error = "x" * 1000
        app = _make_fake_app(
            build_failures=[_FakeBuildFailure("bad", long_error)],
        )
        snap = build_runtime_snapshot(app)
        bf_error = snap["startup"]["build_failures"][0]["error"]
        assert len(bf_error) <= _MAX_ERROR_DETAIL_LEN


# ---------------------------------------------------------------------------
# Tests: Bounded size
# ---------------------------------------------------------------------------
class TestBoundedSize:
    """Collections must be capped to prevent unbounded snapshots."""

    def test_adapters_capped(self) -> None:
        """Adapter count is capped at _MAX_ADAPTERS."""
        adapters = {
            f"adapter-{i:04d}": _FakeAdapter(adapter_id=f"adapter-{i:04d}")
            for i in range(_MAX_ADAPTERS + 50)
        }
        app = _make_fake_app(adapters=adapters)
        snap = build_runtime_snapshot(app)
        assert len(snap["adapters"]) <= _MAX_ADAPTERS

    def test_routes_capped(self) -> None:
        """Route count is capped at _MAX_ROUTES."""
        routes = {f"route-{i:04d}": {"delivered": i} for i in range(_MAX_ROUTES + 50)}
        app = _make_fake_app(route_stats=_FakeRouteStats(routes))
        snap = build_runtime_snapshot(app)
        assert len(snap["routes"]["stats"]["per_route"]) <= _MAX_ROUTES

    def test_build_failures_capped(self) -> None:
        """Build failure count is capped at _MAX_BUILD_FAILURES."""
        failures = [
            _FakeBuildFailure(f"bf-{i}", f"error {i}")
            for i in range(_MAX_BUILD_FAILURES + 20)
        ]
        app = _make_fake_app(build_failures=failures)
        snap = build_runtime_snapshot(app)
        assert len(snap["startup"]["build_failures"]) <= _MAX_BUILD_FAILURES


# ---------------------------------------------------------------------------
# Tests: Representative snapshot contents
# ---------------------------------------------------------------------------
class TestSnapshotContents:
    """Snapshot contains expected sections with correct data."""

    def test_schema_version_present(self) -> None:
        """Schema version matches module constant."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["schema_version"] == SCHEMA_VERSION
        assert snap["schema_version"] == 1

    def test_snapshot_at_is_iso8601(self) -> None:
        """snapshot_at is a valid ISO-8601 UTC timestamp."""
        fixed = datetime(2026, 5, 11, 10, 30, 0, tzinfo=timezone.utc)
        snap = build_runtime_snapshot(
            _make_fake_app(),
            now_fn=lambda: fixed,
        )
        assert snap["snapshot_at"] == "2026-05-11T10:30:00+00:00"

    def test_runtime_state_reflected(self) -> None:
        """runtime_state matches the app's current state."""
        app = _make_fake_app(state=_FakeRuntimeState.RUNNING)
        snap = build_runtime_snapshot(app)
        assert snap["lifecycle"]["runtime_state"] == "running"

    def test_runtime_state_failed(self) -> None:
        app = _make_fake_app(state=_FakeRuntimeState.FAILED)
        snap = build_runtime_snapshot(app)
        assert snap["lifecycle"]["runtime_state"] == "failed"

    def test_adapters_contents(self) -> None:
        """Adapters section contains correct metadata for each adapter."""
        app = _make_fake_app(
            adapters={
                "meshtastic-1": _FakeAdapter(
                    adapter_id="meshtastic-1",
                    platform="meshtastic",
                    role=_FakeRole.TRANSPORT,
                    version="1.2.3",
                    health="healthy",
                ),
                "matrix-1": _FakeAdapter(
                    adapter_id="matrix-1",
                    platform="matrix",
                    role=_FakeRole.PRESENTATION,
                    version="4.5.6",
                    health="degraded",
                ),
            },
        )
        snap = build_runtime_snapshot(app)

        assert "meshtastic-1" in snap["adapters"]
        assert "matrix-1" in snap["adapters"]

        mt = snap["adapters"]["meshtastic-1"]
        assert mt["adapter_id"] == "meshtastic-1"
        assert mt["platform"] == "meshtastic"
        assert mt["role"] == "transport"
        assert mt["version"] == "1.2.3"
        assert mt["health"] == "healthy"
        assert "capabilities" in mt
        assert isinstance(mt["capabilities"], dict)

        mx = snap["adapters"]["matrix-1"]
        assert mx["role"] == "presentation"
        assert mx["health"] == "degraded"

    def test_routes_from_route_stats(self) -> None:
        """Routes stats section mirrors route_stats.snapshot() output."""
        route_data = {
            "bridge-a": {
                "delivered": 10,
                "failed": 1,
                "skipped": 0,
                "loop_prevented": 0,
            },
            "bridge-b": {
                "delivered": 5,
                "failed": 0,
                "skipped": 2,
                "loop_prevented": 1,
            },
        }
        app = _make_fake_app(route_stats=_FakeRouteStats(route_data))
        snap = build_runtime_snapshot(app)

        assert snap["routes"]["stats"]["per_route"]["bridge-a"]["delivered"] == 10
        assert snap["routes"]["stats"]["per_route"]["bridge-b"]["loop_prevented"] == 1

    def test_capacity_state_present(self) -> None:
        """Capacity section contains controller snapshot."""
        cap_data = {
            "accepting_work": True,
            "delivery_current": 3,
            "delivery_limit": 50,
            "delivery_rejections": 1,
            "delivery_timeouts": 0,
            "replay_current": 1,
            "replay_limit": 25,
            "replay_rejections": 0,
            "replay_timeouts": 0,
        }
        app = _make_fake_app(capacity_controller=_FakeCapacityController(cap_data))
        snap = build_runtime_snapshot(app)
        assert snap["capacity"]["state"]["delivery_current"] == 3
        assert snap["capacity"]["state"]["delivery_limit"] == 50
        assert snap["capacity"]["state"]["accepting_work"] is True

    def test_limits_reflected(self) -> None:
        """Limits section matches the config's RuntimeLimits."""
        app = _make_fake_app(
            config=_FakeRuntimeConfig(
                limits=_FakeRuntimeLimits(
                    max_inflight_deliveries=100,
                    max_inflight_replay_events=50,
                    shutdown_drain_timeout_seconds=15,
                    delivery_acquire_timeout_seconds=3.0,
                ),
            )
        )
        snap = build_runtime_snapshot(app)
        assert snap["limits"]["max_inflight_deliveries"] == 100
        assert snap["limits"]["max_inflight_replay_events"] == 50
        assert snap["limits"]["shutdown_drain_timeout_seconds"] == 15
        assert snap["limits"]["delivery_acquire_timeout_seconds"] == 3.0

    def test_replay_available_true(self) -> None:
        """Replay available is true when replay engine is present."""
        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {"total": 1}}),
        )
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"]["global"]["total"] == 1

    def test_replay_available_false(self) -> None:
        """Replay available is false when no replay engine."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is False
        assert snap["replay"]["counters"] is None

    def test_build_failures_included(self) -> None:
        """Build failures are included in the startup section."""
        app = _make_fake_app(
            build_failures=[
                _FakeBuildFailure("bad-1", "timeout"),
                _FakeBuildFailure("bad-2", "connection refused"),
            ],
        )
        snap = build_runtime_snapshot(app)
        assert len(snap["startup"]["build_failures"]) == 2
        assert snap["startup"]["build_failures"][0]["adapter_id"] == "bad-1"
        assert snap["startup"]["build_failures"][1]["error"] == "connection refused"


# ---------------------------------------------------------------------------
# Tests: Startup timestamp & uptime
# ---------------------------------------------------------------------------
class TestStartupTimestampAndUptime:
    """Startup timestamp and uptime computation (now in lifecycle section)."""

    def test_startup_fields_present_when_set(self) -> None:
        """When startup fields exist on the app, they are reflected in lifecycle."""
        app = _make_fake_app(
            startup_wall="2026-05-11T10:00:00+00:00",
            startup_monotonic=1000.0,
        )
        snap = build_runtime_snapshot(
            app,
            monotonic_fn=lambda: 1362.5,
        )
        assert snap["lifecycle"]["startup_timestamp"] == "2026-05-11T10:00:00+00:00"
        assert snap["lifecycle"]["uptime_seconds"] == 362.5

    def test_startup_fields_null_when_absent(self) -> None:
        """When startup fields are not on the app, both are null."""
        app = _make_fake_app()  # no startup fields
        snap = build_runtime_snapshot(app)
        assert snap["lifecycle"]["startup_timestamp"] is None
        assert snap["lifecycle"]["uptime_seconds"] is None

    def test_uptime_rounded_to_microseconds(self) -> None:
        """Uptime is rounded to 6 decimal places."""
        app = _make_fake_app(startup_monotonic=100.0)
        snap = build_runtime_snapshot(
            app,
            monotonic_fn=lambda: 200.123456789,
        )
        assert snap["lifecycle"]["uptime_seconds"] == 100.123457  # rounded to 6 places

    def test_uptime_clamped_to_zero(self) -> None:
        """Negative uptime is clamped to 0.0."""
        app = _make_fake_app(startup_monotonic=1000.0)
        snap = build_runtime_snapshot(
            app,
            monotonic_fn=lambda: 500.0,  # earlier than startup
        )
        assert snap["lifecycle"]["uptime_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Tests: Graceful handling of absent optional structures
# ---------------------------------------------------------------------------
class TestGracefulAbsence:
    """Missing optional subsystems must not raise errors."""

    def test_no_route_stats_gives_empty_routes(self) -> None:
        """When route_stats is None, routes stats is empty dict."""
        app = _make_fake_app(route_stats=None)
        snap = build_runtime_snapshot(app)
        assert snap["routes"]["stats"]["per_route"] == {}

    def test_no_capacity_gives_null_state(self) -> None:
        app = _make_fake_app(capacity_controller=None)
        snap = build_runtime_snapshot(app)
        assert snap["capacity"]["state"] is None

    def test_no_replay_engine_gives_false_availability(self) -> None:
        app = _make_fake_app(replay_engine=None)
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is False
        assert snap["replay"]["counters"] is None

    def test_no_diagnostics_collector_gives_null_replay_counters(self) -> None:
        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=None,
        )
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"] is None

    def test_no_health_state_gives_null_startup_health(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] is None

    def test_no_config_gives_empty_limits(self) -> None:
        """When config is missing, limits is an empty dict."""
        app = _make_fake_app(config=None)
        snap = build_runtime_snapshot(app)
        assert snap["limits"] == {}

    def test_no_limits_on_config_gives_empty_limits(self) -> None:
        """When config has no limits attribute, limits is empty."""
        app = _make_fake_app(config=object())  # bare object has no limits
        snap = build_runtime_snapshot(app)
        assert snap["limits"] == {}

    def test_empty_adapters_gives_empty_dict(self) -> None:
        app = _make_fake_app(adapters={})
        snap = build_runtime_snapshot(app)
        assert snap["adapters"] == {}

    def test_no_adapters_attr_gives_empty_dict(self) -> None:
        """When app has no adapters attribute, adapters is empty."""
        snap = build_runtime_snapshot(object())
        assert snap["adapters"] == {}

    def test_minimal_app_object_works(self) -> None:
        """Even a bare object() doesn't crash the snapshot."""
        snap = build_runtime_snapshot(object())
        assert "schema_version" in snap
        assert "lifecycle" in snap


# ---------------------------------------------------------------------------
# Tests: Startup health state tolerance
# ---------------------------------------------------------------------------
class TestHealthStateTolerance:
    """Startup health state is null when absent, dict when present."""

    def test_startup_health_state_dict(self) -> None:
        app = _make_fake_app(health_state={"overall": "healthy", "adapters": 3})
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] == {
            "overall": "healthy",
            "adapters": 3,
        }

    def test_startup_health_state_to_dict(self) -> None:
        class _HS:
            def to_dict(self) -> dict[str, Any]:
                return {"overall": "degraded"}

        app = _make_fake_app(health_state=_HS())
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] == {"overall": "degraded"}

    def test_startup_health_non_dict_non_to_dict_gives_null(self) -> None:
        app = _make_fake_app(health_state="just_a_string")
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] is None


class TestLiveHealthExplicitlyUnavailable:
    """live_health is null before manual refresh_live_health() is called.

    Before an explicit call to :meth:`MedreApp.refresh_live_health`, the
    ``health.live_health`` field is ``null`` and ``health.scope`` is
    ``"startup"``.  This is not a missing feature — it is the correct
    default state reflecting that no live health poll has been performed.
    After ``refresh_live_health()`` is called, ``live_health`` is populated
    with a :class:`LiveHealthSnapshot` and ``scope`` becomes ``"live"``.
    """

    def test_live_health_is_null_when_no_live_health_state(self) -> None:
        """live_health is null when _live_health_state is not set."""
        app = _make_fake_app(health_state={"overall": "healthy"})
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is None

    def test_live_health_is_null_when_health_state_absent(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is None

    def test_live_health_is_null_for_minimal_app(self) -> None:
        snap = build_runtime_snapshot(object())
        assert snap["health"]["live_health"] is None

    def test_live_health_scope_startup_before_refresh(self) -> None:
        """Before refresh, health.scope is 'startup' and live_refresh is False."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["health"]["scope"] == "startup"
        assert snap["health"]["live_refresh"] is False


class TestLiveHealthPopulated:
    """live_health is populated when _live_health_state is present."""

    def test_live_health_dict_when_state_present(self) -> None:
        """When _live_health_state has to_dict(), live_health is populated."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth, LiveHealthSnapshot

        adapter_health = AdapterLiveHealth(
            adapter_id="a1",
            health="healthy",
            adapter_state=AdapterState.READY,
            fake_or_live="fake",
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
        )
        live_snap = LiveHealthSnapshot(
            runtime_health="healthy",
            adapter_summary={
                "healthy": 1,
                "degraded": 0,
                "failed": 0,
                "transitional": 0,
                "total": 1,
            },
            adapters={"a1": adapter_health},
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
            poll_count=1,
        )
        app = _make_fake_app()
        app._live_health_state = live_snap  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is not None
        assert snap["health"]["live_health"]["runtime_health"] == "healthy"
        assert snap["health"]["live_health"]["poll_count"] == 1
        assert "a1" in snap["health"]["live_health"]["adapters"]
        assert snap["health"]["scope"] == "live"
        assert snap["health"]["live_refresh"] is True

    def test_startup_health_unchanged_after_live_refresh(self) -> None:
        """startup.startup_health remains unchanged when live health is populated."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth, LiveHealthSnapshot

        adapter_health = AdapterLiveHealth(
            adapter_id="a1",
            health="healthy",
            adapter_state=AdapterState.READY,
            fake_or_live="fake",
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
        )
        live_snap = LiveHealthSnapshot(
            runtime_health="healthy",
            adapter_summary={
                "healthy": 1,
                "degraded": 0,
                "failed": 0,
                "transitional": 0,
                "total": 1,
            },
            adapters={"a1": adapter_health},
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
            poll_count=1,
        )
        startup_health_val = {
            "runtime_health": "healthy",
            "adapter_summary": {"total": 1},
        }
        app = _make_fake_app(health_state=startup_health_val)
        app._live_health_state = live_snap  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        # startup_health unchanged
        assert snap["startup"]["startup_health"] == startup_health_val
        # live_health populated
        assert snap["health"]["live_health"] is not None
        # schema version unchanged
        assert snap["schema_version"] == 1


# ---------------------------------------------------------------------------
# Tests: Injected clocks for testability
# ---------------------------------------------------------------------------
class TestInjectedClocks:
    """now_fn and monotonic_fn are properly used."""

    def test_fixed_now_fn(self) -> None:
        fixed = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        snap = build_runtime_snapshot(
            _make_fake_app(),
            now_fn=lambda: fixed,
        )
        assert snap["snapshot_at"] == "2025-01-01T00:00:00+00:00"

    def test_fixed_monotonic_fn_for_uptime(self) -> None:
        app = _make_fake_app(startup_monotonic=0.0)
        snap = build_runtime_snapshot(
            app,
            monotonic_fn=lambda: 60.0,
        )
        assert snap["lifecycle"]["uptime_seconds"] == 60.0


# ---------------------------------------------------------------------------
# Tests: Route stats integration
# ---------------------------------------------------------------------------
class TestRouteStatsIntegration:
    """Route stats snapshot data flows correctly into the snapshot."""

    def test_route_stats_with_last_error(self) -> None:
        route_data = {
            "r1": {
                "delivered": 5,
                "failed": 1,
                "skipped": 0,
                "loop_prevented": 0,
                "last_error": "connection refused",
            },
        }
        app = _make_fake_app(route_stats=_FakeRouteStats(route_data))
        snap = build_runtime_snapshot(app)
        assert (
            snap["routes"]["stats"]["per_route"]["r1"]["last_error"]
            == "connection refused"
        )

    def test_route_stats_sorted_by_route_id(self) -> None:
        route_data = {
            "zebra-route": {"delivered": 1},
            "alpha-route": {"delivered": 2},
        }
        app = _make_fake_app(route_stats=_FakeRouteStats(route_data))
        snap = build_runtime_snapshot(app)
        route_keys = list(snap["routes"]["stats"]["per_route"].keys())
        assert route_keys == ["alpha-route", "zebra-route"]


# ---------------------------------------------------------------------------
# Tests: Capacity integration
# ---------------------------------------------------------------------------
class TestCapacityIntegration:
    """Capacity controller snapshot data flows correctly."""

    def test_capacity_snapshot_included(self) -> None:
        cap = _FakeCapacityController(
            {
                "accepting_work": False,
                "delivery_current": 10,
                "delivery_limit": 100,
                "delivery_rejections": 5,
                "delivery_timeouts": 2,
                "replay_current": 3,
                "replay_limit": 50,
                "replay_rejections": 1,
                "replay_timeouts": 0,
            }
        )
        app = _make_fake_app(capacity_controller=cap)
        snap = build_runtime_snapshot(app)
        assert snap["capacity"]["state"]["accepting_work"] is False
        assert snap["capacity"]["state"]["delivery_current"] == 10
        assert snap["capacity"]["state"]["replay_current"] == 3

    def test_capacity_state_null_when_absent(self) -> None:
        app = _make_fake_app(capacity_controller=None)
        snap = build_runtime_snapshot(app)
        assert snap["capacity"]["state"] is None


# ---------------------------------------------------------------------------
# Tests: Replay integration
# ---------------------------------------------------------------------------
class TestReplayIntegration:
    """Replay engine and metrics flow correctly."""

    def test_replay_counters_from_diagnostics(self) -> None:
        replay_data = {
            "global": {"replay_deliveries_succeeded": 42},
            "by_route": {"bridge-a": {"deliveries_succeeded": 30}},
        }
        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector(replay_data),
        )
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"]["global"]["replay_deliveries_succeeded"] == 42

    def test_replay_available_but_no_counters(self) -> None:
        """Replay available but no diagnostics collector → null counters."""
        app = _make_fake_app(
            replay_engine=_FakeReplayEngine(),
        )
        snap = build_runtime_snapshot(app)
        assert snap["replay"]["available"] is True
        assert snap["replay"]["counters"] is None


# ---------------------------------------------------------------------------
# Tests: Diagnostician fallback path
# ---------------------------------------------------------------------------
class TestDiagnosticianFallback:
    """Snapshot checks _diagnostics_collector then diagnostician."""

    def test_uses_diagnostician_attr_as_fallback(self) -> None:
        """When _diagnostics_collector is absent, uses diagnostician."""

        @dataclass
        class _AppWithDiagnostician:
            adapters: dict[str, Any] = field(default_factory=dict)
            state: Any = _FakeRuntimeState.RUNNING
            route_stats: Any = None
            _capacity_controller: Any = None
            _replay_engine: Any = _FakeReplayEngine()
            config: Any = field(default_factory=_FakeRuntimeConfig)
            build_failures: list[Any] = field(default_factory=list)
            _diagnostics_collector: Any = None
            diagnostician: Any = None

        app = _AppWithDiagnostician(
            diagnostician=_FakeDiagnosticsCollector({"global": {"total": 7}}),
        )

        snap = build_runtime_snapshot(app)
        assert snap["replay"]["counters"]["global"]["total"] == 7


# ---------------------------------------------------------------------------
# Tests: Accounting and BootSummary integration
# ---------------------------------------------------------------------------
class TestAccountingInSnapshot:
    """Runtime accounting counters appear in the snapshot when wired."""

    def test_accounting_absent_when_not_wired(self) -> None:
        """No _runtime_accounting on app → accounting.counters is null."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["accounting"]["counters"] is None

    def test_accounting_present_when_wired(self) -> None:
        """RuntimeAccounting wired → snapshot includes counters."""
        from medre.core.supervision.accounting import RuntimeAccounting

        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()

        app = _make_fake_app()
        # Wire accounting onto the fake app.
        app._runtime_accounting = acc  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        assert snap["accounting"]["counters"] is not None
        assert snap["accounting"]["counters"]["inbound_accepted"] == 2
        assert snap["accounting"]["counters"]["outbound_attempts"] == 1
        assert snap["accounting"]["counters"]["outbound_delivered"] == 0

    def test_accounting_keys_sorted(self) -> None:
        """Accounting dict keys are sorted."""
        from medre.core.supervision.accounting import RuntimeAccounting

        acc = RuntimeAccounting()
        app = _make_fake_app()
        app._runtime_accounting = acc  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        keys = list(snap["accounting"]["counters"].keys())
        assert keys == sorted(keys)


class TestBootSummaryInSnapshot:
    """Boot summary appears in the snapshot when wired."""

    def test_boot_summary_absent_when_not_started(self) -> None:
        """No _boot_summary on app → boot_summary is null."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["boot_summary"] is None

    def test_boot_summary_present_when_wired(self) -> None:
        """BootSummary wired → snapshot includes boot summary."""
        from medre.runtime.boot_summary import build_boot_summary

        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=2,
            adapters_failed=0,
            adapters_total=2,
            adapters_disabled=1,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a1", "a2"],
            route_count=3,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=42,
        )
        app = _make_fake_app()
        app._boot_summary = bs  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        assert snap["startup"]["boot_summary"] is not None
        assert snap["startup"]["boot_summary"]["startup_outcome"] == "success"
        assert snap["startup"]["boot_summary"]["runtime_health"] == "healthy"
        assert snap["startup"]["boot_summary"]["adapters_started"] == 2
        assert snap["startup"]["boot_summary"]["route_count"] == 3
        assert snap["startup"]["boot_summary"]["storage_backend"] == "sqlite"
        assert snap["startup"]["boot_summary"]["persisted_events_count"] == 42

    def test_boot_summary_keys_sorted(self) -> None:
        """Boot summary dict keys are sorted."""
        from medre.runtime.boot_summary import build_boot_summary

        bs = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=1,
            adapters_failed=1,
            adapters_total=2,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=["bad"],
            started_adapter_ids=["ok"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        app = _make_fake_app()
        app._boot_summary = bs  # type: ignore[attr-defined]

        snap = build_runtime_snapshot(app)
        keys = list(snap["startup"]["boot_summary"].keys())
        assert keys == sorted(keys)

    def test_boot_summary_json_safe(self) -> None:
        """Boot summary is JSON-serialisable."""
        from medre.runtime.boot_summary import build_boot_summary

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
            started_adapter_ids=["a1"],
            route_count=0,
            storage_backend="sqlite",
            replay_available=False,
            persisted_events_count=None,
        )
        serialized = json.dumps(bs.to_dict(), sort_keys=True)
        assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# Tests: Provenance metadata (scope + live_refresh)
# ---------------------------------------------------------------------------
class TestProvenanceMetadata:
    """Snapshot sections carry scope and live_refresh provenance metadata.

    Operators use these fields to distinguish startup-derived data from
    process-local data from live data.  The pattern mirrors the existing
    routes.* sub-section provenance convention.
    """

    # -- startup section -------------------------------------------------------

    def test_startup_has_scope_startup(self) -> None:
        """startup.scope is 'startup'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["startup"]["scope"] == "startup"

    def test_startup_has_live_refresh_false(self) -> None:
        """startup.live_refresh is False — computed once, not refreshed."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["startup"]["live_refresh"] is False

    # -- health section --------------------------------------------------------

    def test_health_has_scope_startup_before_refresh(self) -> None:
        """health.scope is 'startup' before live health refresh."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["health"]["scope"] == "startup"

    def test_health_has_live_refresh_false_before_refresh(self) -> None:
        """health.live_refresh is False before live health refresh."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["health"]["live_refresh"] is False

    def test_health_live_health_still_null_before_refresh(self) -> None:
        """health.live_health is None before refresh_live_health is called."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["health"]["live_health"] is None

    # -- lifecycle section -----------------------------------------------------

    def test_lifecycle_has_scope_process_local(self) -> None:
        """lifecycle.scope is 'process_local'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["lifecycle"]["scope"] == "process_local"

    def test_lifecycle_has_live_refresh_false(self) -> None:
        """lifecycle.live_refresh is False — state is current at snapshot time.

        Note: lifecycle.runtime_state and lifecycle.adapters reflect the
        in-process state at the moment of the snapshot call, but the scope is
        'process_local' (not 'live') because it is not periodically refreshed
        by a health polling loop.
        """
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["lifecycle"]["live_refresh"] is False

    # -- diagnostics section ---------------------------------------------------

    def test_diagnostics_has_scope_process_local(self) -> None:
        """diagnostics.scope is 'process_local'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["diagnostics"]["scope"] == "process_local"

    def test_diagnostics_has_live_refresh_false(self) -> None:
        """diagnostics.live_refresh is False — event buffer grows from local
        runtime state transitions, not external adapter polling."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["diagnostics"]["live_refresh"] is False

    # -- per-adapter provenance ------------------------------------------------

    def test_adapter_entry_has_provenance_startup(self) -> None:
        """Each adapter entry carries provenance='startup'."""
        app = _make_fake_app(
            adapters={
                "a1": _FakeAdapter(adapter_id="a1"),
                "b2": _FakeAdapter(adapter_id="b2"),
            },
        )
        snap = build_runtime_snapshot(app)
        for adapter_id, entry in snap["adapters"].items():
            assert (
                entry["provenance"] == "startup"
            ), f"Adapter {adapter_id!r} missing provenance='startup'"

    def test_adapter_provenance_json_safe(self) -> None:
        """Snapshot with adapter provenance is JSON-serialisable."""
        app = _make_fake_app(
            adapters={"x": _FakeAdapter(adapter_id="x")},
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap, sort_keys=True)
        assert '"provenance"' in serialized

    # -- accounting section ----------------------------------------------------

    def test_accounting_has_scope_process_local(self) -> None:
        """accounting.scope is 'process_local'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["accounting"]["scope"] == "process_local"

    def test_accounting_has_live_refresh_false(self) -> None:
        """accounting.live_refresh is False — counters evolve locally."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["accounting"]["live_refresh"] is False

    def test_accounting_counters_key(self) -> None:
        """accounting.counters holds the accounting snapshot (or null)."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "counters" in snap["accounting"]

    # -- capacity section -----------------------------------------------------

    def test_capacity_has_scope_process_local(self) -> None:
        """capacity.scope is 'process_local'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["capacity"]["scope"] == "process_local"

    def test_capacity_has_live_refresh_false(self) -> None:
        """capacity.live_refresh is False — state evolves locally."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["capacity"]["live_refresh"] is False

    def test_capacity_state_key(self) -> None:
        """capacity.state holds the capacity snapshot (or null)."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "state" in snap["capacity"]

    # -- routes.stats section -------------------------------------------------

    def test_routes_stats_has_scope_process_local(self) -> None:
        """routes.stats.scope is 'process_local'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["routes"]["stats"]["scope"] == "process_local"

    def test_routes_stats_has_live_refresh_false(self) -> None:
        """routes.stats.live_refresh is False — counters evolve locally."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["routes"]["stats"]["live_refresh"] is False

    def test_routes_stats_per_route_key(self) -> None:
        """routes.stats.per_route holds the per-route delivery stats."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "per_route" in snap["routes"]["stats"]

    # -- routes section unchanged ----------------------------------------------

    def test_routes_eligibility_retains_scope(self) -> None:
        """routes.eligibility still has scope='build' (unchanged pattern)."""
        snap = build_runtime_snapshot(_make_fake_app())
        # When no route eligibility is wired, the section is None —
        # provenance is at the sub-section level for routes.
        assert snap["routes"]["eligibility"] is None

    # -- provenance does not leak into reserved sections -----------------------

    def test_identity_has_no_scope(self) -> None:
        """identity section does not carry scope (reserved, always empty)."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "scope" not in snap["identity"]

    def test_persistence_has_no_scope(self) -> None:
        """persistence section does not carry scope (reserved, always empty)."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "scope" not in snap["persistence"]

    # -- full round-trip with provenance ---------------------------------------

    def test_full_snapshot_with_provenance_is_json_safe(self) -> None:
        """Complete snapshot including provenance fields round-trips JSON."""
        app = _make_fake_app(
            adapters={"a1": _FakeAdapter(adapter_id="a1")},
            route_stats=_FakeRouteStats({"r1": {"delivered": 5}}),
            capacity_controller=_FakeCapacityController(),
            replay_engine=_FakeReplayEngine(),
            diagnostics_collector=_FakeDiagnosticsCollector({"global": {"total": 1}}),
        )
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap, sort_keys=True)
        assert '"scope"' in serialized
        assert '"live_refresh"' in serialized
        assert '"provenance"' in serialized


# ---------------------------------------------------------------------------
# Live health extension-point type tests
# ---------------------------------------------------------------------------
class TestLiveHealthTypes:
    """Tests for AdapterLiveHealth and LiveHealthSnapshot extension points.

    These types are frozen dataclasses that define the future shape of
    ``health.live_health`` in the runtime snapshot.  They do not implement
    polling — they are pure value types for future integration.
    """

    def test_adapter_live_health_is_frozen(self) -> None:
        """AdapterLiveHealth is frozen (immutable)."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth

        entry = AdapterLiveHealth(
            adapter_id="test-adapter",
            health="healthy",
            adapter_state=AdapterState.READY,
            fake_or_live="live",
            poll_timestamp_monotonic=100.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
        )
        with pytest.raises(AttributeError):
            entry.health = "failed"  # type: ignore[misc]

    def test_adapter_live_health_to_dict_json_safe(self) -> None:
        """AdapterLiveHealth.to_dict() produces JSON-safe output."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth

        entry = AdapterLiveHealth(
            adapter_id="a1",
            health="degraded",
            adapter_state=AdapterState.DEGRADED,
            fake_or_live="live",
            poll_timestamp_monotonic=42.5,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
            error="connection timeout",
        )
        d = entry.to_dict()
        # Must be JSON-safe.
        serialized = json.dumps(d, sort_keys=True)
        assert '"adapter_id"' in serialized
        assert '"degraded"' in serialized
        # Keys must be sorted in the dict output.
        assert list(d.keys()) == sorted(d.keys())
        assert d["adapter_state"] == "degraded"
        assert d["error"] == "connection timeout"

    def test_adapter_live_health_error_defaults_none(self) -> None:
        """AdapterLiveHealth.error defaults to None when omitted."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth

        entry = AdapterLiveHealth(
            adapter_id="a1",
            health="healthy",
            adapter_state=AdapterState.READY,
            fake_or_live="unknown",
            poll_timestamp_monotonic=1.0,
            poll_timestamp_wall="2026-01-01T00:00:00+00:00",
        )
        assert entry.error is None
        assert entry.to_dict()["error"] is None

    def test_live_health_snapshot_is_frozen(self) -> None:
        """LiveHealthSnapshot is frozen (immutable)."""
        from medre.core.supervision.health import LiveHealthSnapshot

        snap = LiveHealthSnapshot(
            runtime_health="healthy",
            adapter_summary={
                "healthy": 1,
                "degraded": 0,
                "failed": 0,
                "transitional": 0,
                "total": 1,
            },
            adapters={},
            poll_timestamp_monotonic=100.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
            poll_count=1,
        )
        with pytest.raises(AttributeError):
            snap.runtime_health = "failed"  # type: ignore[misc]

    def test_live_health_snapshot_to_dict_json_safe(self) -> None:
        """LiveHealthSnapshot.to_dict() produces JSON-safe output with sorted keys."""
        from medre.core.lifecycle.states import AdapterState
        from medre.core.supervision.health import AdapterLiveHealth, LiveHealthSnapshot

        adapter_health = AdapterLiveHealth(
            adapter_id="a1",
            health="healthy",
            adapter_state=AdapterState.READY,
            fake_or_live="live",
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
        )
        snap = LiveHealthSnapshot(
            runtime_health="healthy",
            adapter_summary={
                "healthy": 1,
                "degraded": 0,
                "failed": 0,
                "transitional": 0,
                "total": 1,
            },
            adapters={"a1": adapter_health},
            poll_timestamp_monotonic=50.0,
            poll_timestamp_wall="2026-05-14T10:00:00+00:00",
            poll_count=5,
        )
        d = snap.to_dict()
        serialized = json.dumps(d, sort_keys=True)
        assert '"runtime_health"' in serialized
        assert '"poll_count"' in serialized
        assert d["poll_count"] == 5
        assert "a1" in d["adapters"]
        assert d["adapters"]["a1"]["health"] == "healthy"

    def test_snapshot_live_health_section_still_null(self) -> None:
        """Snapshot health.live_health is still None — types defined but not wired."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["health"]["live_health"] is None
        assert snap["health"]["live_refresh"] is False
        assert snap["health"]["scope"] == "startup"
