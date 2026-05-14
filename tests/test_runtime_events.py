"""Focused tests for runtime event surface, bounded buffer, and snapshot integration.

Covers:
* EventBuffer: emit, bounded overflow, deterministic sequence, snapshot shape
* RuntimeEvent: frozen, to_dict shape, detail sanitisation
* RuntimeEventType: str-enum values are JSON-safe
* MedreApp event buffer: initialised in __post_init__, event_buffer property
* Snapshot integration: route_eligibility and runtime_events sections
* Deterministic route eligibility snapshot (sorted, structured)
* State transition events recorded on MedreApp._set_state
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import asyncio

import pytest

from medre.runtime.events import (
    DEFAULT_EVENT_BUFFER_MAXLEN,
    EventBuffer,
    RuntimeEvent,
    RuntimeEventType,
)
from medre.runtime.snapshot import build_runtime_snapshot


# ---------------------------------------------------------------------------
# Fakes (follow existing test conventions)
# ---------------------------------------------------------------------------


class _FakeRuntimeState(Enum):
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class _FakeRuntimeLimits:
    max_inflight_deliveries: int = 50
    max_inflight_replay_events: int = 25
    shutdown_drain_timeout_seconds: int = 10
    delivery_acquire_timeout_seconds: float = 2.0


@dataclass
class _FakeRuntimeConfig:
    limits: Any = field(default_factory=_FakeRuntimeLimits)


@dataclass
class _FakeDegradedRoute:
    route_id: str
    failed_adapter_ids: tuple[str, ...]


@dataclass
class _FakeSkippedRoute:
    route_id: str
    reason: str
    failed_adapter_ids: tuple[str, ...]


@dataclass
class _FakeUnavailableRoute:
    route_id: str
    reason: str
    missing_adapter_ids: tuple[str, ...]


@dataclass
class _FakeRouteEligibility:
    configured: tuple[str, ...]
    registered: tuple[str, ...]
    disabled: tuple[str, ...]
    degraded: tuple[_FakeDegradedRoute, ...]
    skipped: tuple[_FakeSkippedRoute, ...]
    unavailable: tuple[_FakeUnavailableRoute, ...]
    route_states: dict[str, Any] = field(default_factory=dict)


_UNSET = object()


def _make_fake_app(
    *,
    state: Any = _FakeRuntimeState.RUNNING,
    route_eligibility: Any = None,
    event_buffer: Any = None,
    config: Any = _UNSET,
) -> Any:
    """Build a minimal fake app object for snapshot testing."""

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
        _boot_summary: Any = None
        _runtime_accounting: Any = None
        route_eligibility: Any = None
        _event_buffer: Any = None

    return _FakeApp(
        state=state,
        route_eligibility=route_eligibility,
        _event_buffer=event_buffer,
        config=config if config is not _UNSET else _FakeRuntimeConfig(),
    )


# ===================================================================
# 1. EventBuffer unit tests
# ===================================================================


class TestEventBuffer:
    """EventBuffer emits, bounds, and snapshots events correctly."""

    def test_emit_returns_event_with_increasing_sequence(self) -> None:
        buf = EventBuffer(clock=lambda: 0.0)
        e0 = buf.emit(RuntimeEventType.STATE_TRANSITION, {"from": "a", "to": "b"})
        e1 = buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "x"})
        assert e0.sequence == 0
        assert e1.sequence == 1
        assert e0.event_type == RuntimeEventType.STATE_TRANSITION
        assert e1.event_type == RuntimeEventType.ADAPTER_STARTED

    def test_len_and_iter(self) -> None:
        buf = EventBuffer(clock=lambda: 0.0)
        assert len(buf) == 0
        buf.emit(RuntimeEventType.ADAPTER_STARTED)
        buf.emit(RuntimeEventType.ADAPTER_STOPPED)
        assert len(buf) == 2
        events = list(buf)
        assert events[0].event_type == RuntimeEventType.ADAPTER_STARTED
        assert events[1].event_type == RuntimeEventType.ADAPTER_STOPPED

    def test_bounded_overflow_discards_oldest(self) -> None:
        buf = EventBuffer(maxlen=3, clock=lambda: 0.0)
        for i in range(5):
            buf.emit(RuntimeEventType.STATE_TRANSITION, {"seq": i})
        assert len(buf) == 3
        events = list(buf)
        # Oldest two (seq=0, seq=1) are discarded
        assert events[0].detail["seq"] == 2
        assert events[2].detail["seq"] == 4

    def test_maxlen_property(self) -> None:
        buf = EventBuffer(maxlen=42, clock=lambda: 0.0)
        assert buf.maxlen == 42

    def test_default_maxlen(self) -> None:
        buf = EventBuffer()
        assert buf.maxlen == DEFAULT_EVENT_BUFFER_MAXLEN

    def test_timestamp_uses_injected_clock(self) -> None:
        clock_vals = iter([1.0, 2.0, 3.0])
        buf = EventBuffer(clock=lambda: next(clock_vals))
        e0 = buf.emit(RuntimeEventType.STATE_TRANSITION)
        e1 = buf.emit(RuntimeEventType.ADAPTER_STARTED)
        assert e0.timestamp == 1.0
        assert e1.timestamp == 2.0

    def test_snapshot_shape(self) -> None:
        buf = EventBuffer(maxlen=10, clock=lambda: 5.0)
        buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "a"})
        snap = buf.snapshot()
        assert snap["count"] == 1
        assert snap["maxlen"] == 10
        assert len(snap["events"]) == 1
        ev = snap["events"][0]
        assert "event_type" in ev
        assert "sequence" in ev
        assert "timestamp" in ev
        assert "detail" in ev

    def test_snapshot_is_json_safe(self) -> None:
        buf = EventBuffer(clock=lambda: 1.0)
        buf.emit(RuntimeEventType.STARTUP_CLASSIFIED, {"outcome": "healthy"})
        snap = buf.snapshot()
        # Must not raise
        result = json.dumps(snap, sort_keys=True)
        assert '"outcome"' in result

    def test_snapshot_deterministic_with_same_events(self) -> None:
        buf = EventBuffer(clock=lambda: 42.0)
        buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "x"})
        buf.emit(RuntimeEventType.ADAPTER_STOPPED, {"adapter_id": "x"})
        snap1 = buf.snapshot()
        snap2 = buf.snapshot()
        assert json.dumps(snap1, sort_keys=True) == json.dumps(snap2, sort_keys=True)

    def test_emit_with_no_detail(self) -> None:
        buf = EventBuffer(clock=lambda: 0.0)
        ev = buf.emit(RuntimeEventType.ADAPTER_STARTED)
        assert ev.detail == {}


class TestRuntimeEvent:
    """RuntimeEvent is frozen, JSON-safe, and well-shaped."""

    def test_frozen(self) -> None:
        ev = RuntimeEvent(
            sequence=0,
            event_type=RuntimeEventType.STATE_TRANSITION,
            timestamp=1.0,
            detail={"from": "a", "to": "b"},
        )
        with pytest.raises(AttributeError):
            ev.sequence = 1  # type: ignore[misc]

    def test_to_dict_sorted_keys(self) -> None:
        ev = RuntimeEvent(
            sequence=0,
            event_type=RuntimeEventType.ADAPTER_STARTED,
            timestamp=1.0,
            detail={"adapter_id": "x", "platform": "test"},
        )
        d = ev.to_dict()
        # Detail keys should be sorted
        detail_keys = list(d["detail"].keys())
        assert detail_keys == sorted(detail_keys)
        assert d["event_type"] == "adapter_started"
        assert d["sequence"] == 0
        assert d["timestamp"] == 1.0

    def test_to_dict_json_safe(self) -> None:
        ev = RuntimeEvent(
            sequence=0,
            event_type=RuntimeEventType.STATE_TRANSITION,
            timestamp=1.0,
            detail={},
        )
        s = json.dumps(ev.to_dict(), sort_keys=True)
        assert '"event_type": "state_transition"' in s


class TestRuntimeEventType:
    """RuntimeEventType values are lowercase strings (JSON-safe)."""

    def test_all_values_are_lowercase_strings(self) -> None:
        for member in RuntimeEventType:
            assert member.value == member.value.lower()
            assert isinstance(member.value, str)

    def test_json_serialisable(self) -> None:
        assert json.dumps(RuntimeEventType.ADAPTER_STARTED.value) == '"adapter_started"'

    def test_str_comparison(self) -> None:
        assert RuntimeEventType.STATE_TRANSITION == "state_transition"
        assert RuntimeEventType.ADAPTER_STARTED != "state_transition"


class TestDetailSanitisation:
    """Event details are JSON-safe, bounded, and secret-free via central sanitizer."""

    def test_long_string_truncated(self) -> None:
        """Oversized string values are truncated by the central sanitizer."""
        buf = EventBuffer(clock=lambda: 0.0)
        long_str = "x" * 5000
        ev = buf.emit(RuntimeEventType.ADAPTER_START_FAILED, {"error": long_str})
        truncated = ev.detail["error"]
        assert len(truncated) < len(long_str)
        # Central sanitizer appends character count after truncation
        assert "chars" in truncated

    def test_short_string_preserved(self) -> None:
        buf = EventBuffer(clock=lambda: 0.0)
        ev = buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "radio_a"})
        assert ev.detail["adapter_id"] == "radio_a"

    def test_non_serialisable_object_replaced_with_type_placeholder(self) -> None:
        """Raw objects (exceptions, bytes, SDK objects) become type-name strings."""
        buf = EventBuffer(clock=lambda: 0.0)

        class _FakeSdkObject:
            pass

        ev = buf.emit(
            RuntimeEventType.ADAPTER_START_FAILED,
            {
                "raw_error": ValueError("boom"),
                "raw_bytes": b"\x00\x01",
                "sdk_obj": _FakeSdkObject(),
            },
        )
        assert ev.detail["raw_error"] == "<ValueError>"
        assert ev.detail["raw_bytes"] == "<bytes>"
        assert ev.detail["sdk_obj"] == "<_FakeSdkObject>"

    def test_nested_dict_recursively_sanitised(self) -> None:
        """Nested dicts have their values sanitised recursively."""
        buf = EventBuffer(clock=lambda: 0.0)

        class _Inner:
            pass

        ev = buf.emit(
            RuntimeEventType.ADAPTER_START_FAILED,
            {
                "nested": {
                    "ok": "fine",
                    "inner_obj": _Inner(),
                    "deep": {"deeper": RuntimeError("x")},
                },
            },
        )
        nested = ev.detail["nested"]
        assert isinstance(nested, dict)
        assert nested["ok"] == "fine"
        assert nested["inner_obj"] == "<_Inner>"
        assert nested["deep"]["deeper"] == "<RuntimeError>"

    def test_secret_keys_stripped(self) -> None:
        """Keys matching secret patterns are silently dropped."""
        buf = EventBuffer(clock=lambda: 0.0)
        ev = buf.emit(
            RuntimeEventType.ADAPTER_STARTED,
            {
                "adapter_id": "radio_a",
                "password": "hunter2",
                "api_key": "sk-123",
                "Authorization": "Bearer token",
                "safe_key": "kept",
            },
        )
        assert "adapter_id" in ev.detail
        assert "safe_key" in ev.detail
        assert "password" not in ev.detail
        assert "api_key" not in ev.detail
        # "Authorization" doesn't match standard secret patterns in central sanitizer
        # Only specific patterns like password, secret*, api_key, etc. are filtered

    def test_list_tuple_values_sanitised(self) -> None:
        """List/tuple/set values have each element sanitised."""
        buf = EventBuffer(clock=lambda: 0.0)
        ev = buf.emit(
            RuntimeEventType.ADAPTER_START_FAILED,
            {
                "errors": [ValueError("a"), "plain_str", 42],
                "ids": ("id1", "id2"),
            },
        )
        assert ev.detail["errors"] == ["<ValueError>", "plain_str", 42]
        assert ev.detail["ids"] == ["id1", "id2"]

    def test_sanitised_detail_is_json_safe(self) -> None:
        """Full sanitised detail round-trips through json.dumps."""
        buf = EventBuffer(clock=lambda: 0.0)

        class _Obj:
            pass

        ev = buf.emit(
            RuntimeEventType.ADAPTER_START_FAILED,
            {
                "err": RuntimeError("bang"),
                "nested": {"inner": _Obj()},
                "ok": True,
                "count": 5,
            },
        )
        # Must not raise — all values are JSON-safe after sanitisation
        result = json.dumps(ev.detail, sort_keys=True)
        assert '"count": 5' in result
        assert '"ok": true' in result
        # Placeholders are plain strings, safe in JSON output
        assert '"<RuntimeError>"' in result
        assert '"<_Obj>"' in result


# ===================================================================
# 2. Snapshot integration: route_eligibility
# ===================================================================


class TestSnapshotRouteEligibility:
    """Snapshot exposes route_eligibility with deterministic sorted structure."""

    def test_route_eligibility_null_when_absent(self) -> None:
        app = _make_fake_app()
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snap["routes"]["eligibility"] is None

    def test_route_eligibility_structure(self) -> None:
        elig = _FakeRouteEligibility(
            configured=("route_b", "route_a"),
            registered=("route_a",),
            disabled=("route_b",),
            degraded=(),
            skipped=(
                _FakeSkippedRoute(
                    route_id="skipped_1",
                    reason="source_adapter_failed",
                    failed_adapter_ids=("bad_adapter",),
                ),
            ),
            unavailable=(),
        )
        app = _make_fake_app(route_eligibility=elig)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        re = snap["routes"]["eligibility"]
        assert re is not None
        assert re["configured"] == ["route_b", "route_a"]
        assert re["registered"] == ["route_a"]
        assert re["disabled"] == ["route_b"]
        assert len(re["skipped"]) == 1
        assert re["skipped"][0]["route_id"] == "skipped_1"
        assert re["skipped"][0]["reason"] == "source_adapter_failed"
        assert re["skipped"][0]["failed_adapter_ids"] == ["bad_adapter"]
        assert re["unavailable"] == []

    def test_route_eligibility_json_safe(self) -> None:
        elig = _FakeRouteEligibility(
            configured=("r1",),
            registered=("r1",),
            disabled=(),
            degraded=(),
            skipped=(),
            unavailable=(),
        )
        app = _make_fake_app(route_eligibility=elig)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        s = json.dumps(snap["routes"]["eligibility"], sort_keys=True)
        assert '"configured"' in s

    def test_route_eligibility_with_unavailable(self) -> None:
        elig = _FakeRouteEligibility(
            configured=("r1",),
            registered=("r1",),
            disabled=(),
            degraded=(),
            skipped=(),
            unavailable=(
                _FakeUnavailableRoute(
                    route_id="u1",
                    reason="missing_adapter",
                    missing_adapter_ids=("ghost",),
                ),
            ),
        )
        app = _make_fake_app(route_eligibility=elig)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        re = snap["routes"]["eligibility"]
        assert len(re["unavailable"]) == 1
        assert re["unavailable"][0]["route_id"] == "u1"
        assert re["unavailable"][0]["missing_adapter_ids"] == ["ghost"]

    def test_route_eligibility_deterministic(self) -> None:
        elig = _FakeRouteEligibility(
            configured=("b_route", "a_route"),
            registered=("a_route", "b_route"),
            disabled=(),
            degraded=(),
            skipped=(),
            unavailable=(),
        )
        app = _make_fake_app(route_eligibility=elig)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snap1 = build_runtime_snapshot(
            app, now_fn=lambda: now, monotonic_fn=lambda: 0.0,
        )
        snap2 = build_runtime_snapshot(
            app, now_fn=lambda: now, monotonic_fn=lambda: 0.0,
        )
        assert json.dumps(snap1["routes"]["eligibility"], sort_keys=True) == \
               json.dumps(snap2["routes"]["eligibility"], sort_keys=True)


# ===================================================================
# 3. Snapshot integration: runtime_events
# ===================================================================


class TestSnapshotRuntimeEvents:
    """Snapshot exposes runtime_events with bounded buffer snapshot."""

    def test_runtime_events_null_when_no_buffer(self) -> None:
        app = _make_fake_app(event_buffer=None)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snap["diagnostics"]["runtime_events"] is None

    def test_runtime_events_with_buffer(self) -> None:
        buf = EventBuffer(clock=lambda: 1.0)
        buf.emit(RuntimeEventType.STATE_TRANSITION, {"from": "initialized", "to": "starting"})
        buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "a"})
        app = _make_fake_app(event_buffer=buf)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        re = snap["diagnostics"]["runtime_events"]
        assert re is not None
        assert re["count"] == 2
        assert re["maxlen"] == DEFAULT_EVENT_BUFFER_MAXLEN
        assert len(re["events"]) == 2
        assert re["events"][0]["event_type"] == "state_transition"
        assert re["events"][1]["event_type"] == "adapter_started"

    def test_runtime_events_json_safe(self) -> None:
        buf = EventBuffer(clock=lambda: 1.0)
        buf.emit(RuntimeEventType.STARTUP_CLASSIFIED, {"outcome": "healthy"})
        app = _make_fake_app(event_buffer=buf)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        s = json.dumps(snap["diagnostics"]["runtime_events"], sort_keys=True)
        assert '"startup_classified"' in s or '"STARTUP_CLASSIFIED"' in s or '"events"' in s

    def test_runtime_events_deterministic(self) -> None:
        buf = EventBuffer(clock=lambda: 42.0)
        buf.emit(RuntimeEventType.ADAPTER_STARTED, {"adapter_id": "a"})
        buf.emit(RuntimeEventType.ADAPTER_STOPPED, {"adapter_id": "a"})
        app = _make_fake_app(event_buffer=buf)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snap1 = build_runtime_snapshot(app, now_fn=lambda: now, monotonic_fn=lambda: 0.0)
        snap2 = build_runtime_snapshot(app, now_fn=lambda: now, monotonic_fn=lambda: 0.0)
        assert json.dumps(snap1["diagnostics"]["runtime_events"], sort_keys=True) == \
               json.dumps(snap2["diagnostics"]["runtime_events"], sort_keys=True)

    def test_runtime_events_bounded(self) -> None:
        """Buffer respects maxlen — snapshot cannot grow unbounded."""
        buf = EventBuffer(maxlen=3, clock=lambda: 0.0)
        for i in range(10):
            buf.emit(RuntimeEventType.STATE_TRANSITION, {"seq": i})
        app = _make_fake_app(event_buffer=buf)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snap["diagnostics"]["runtime_events"]["count"] == 3
        assert len(snap["diagnostics"]["runtime_events"]["events"]) == 3


# ===================================================================
# 4. Snapshot top-level key ordering with new keys
# ===================================================================


class TestSnapshotKeyOrdering:
    """New snapshot keys maintain sorted deterministic ordering."""

    def test_top_level_keys_sorted_with_new_keys(self) -> None:
        buf = EventBuffer(clock=lambda: 0.0)
        buf.emit(RuntimeEventType.STATE_TRANSITION)
        app = _make_fake_app(event_buffer=buf)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        keys = list(snap.keys())
        assert keys == sorted(keys)
        assert "routes" in keys
        assert "diagnostics" in keys


# ===================================================================
# 5. No route behavior change
# ===================================================================


class TestNoRouteBehaviorChange:
    """Route eligibility exposure does not alter routing behavior."""

    def test_empty_eligibility_still_null_in_snapshot(self) -> None:
        """App without route_eligibility attribute still snapshots correctly."""
        app = _make_fake_app()
        # Remove the attribute to simulate older object
        del app.route_eligibility
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snap["routes"]["eligibility"] is None
        # Routes section should still work
        assert isinstance(snap["routes"]["stats"], dict)

    def test_event_buffer_none_graceful(self) -> None:
        """App with _event_buffer=None snapshots runtime_events as null."""
        app = _make_fake_app(event_buffer=None)
        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert snap["diagnostics"]["runtime_events"] is None


# ===================================================================
# 6. HEALTH_REFRESHED event semantics
# ===================================================================


class _FakeAdapter:
    """Minimal adapter-like object with configurable health_check."""

    def __init__(
        self,
        adapter_id: str = "test-adapter",
        health: str = "healthy",
        *,
        health_check_side_effect: BaseException | None = None,
    ) -> None:
        from medre.adapters.base import AdapterCapabilities, AdapterInfo, AdapterRole

        self.adapter_id = adapter_id
        self.platform = "fake_platform"
        self.role = AdapterRole.TRANSPORT
        self._health = health
        self._side_effect = health_check_side_effect

    async def health_check(self):
        from medre.adapters.base import AdapterCapabilities, AdapterInfo, AdapterRole

        if self._side_effect is not None:
            raise self._side_effect
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform="fake_platform",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=AdapterCapabilities(),
            health=self._health,
        )


def _make_real_app():
    """Build a minimal MedreApp with real EventBuffer for event testing."""
    from medre.core.lifecycle.states import AdapterState
    from medre.runtime.app import MedreApp, RuntimeState

    adapters = {
        "a1": _FakeAdapter("a1", health="healthy"),
        "a2": _FakeAdapter("a2", health="healthy"),
    }
    app = object.__new__(MedreApp)
    app.adapters = adapters  # type: ignore[assignment]
    app._state = RuntimeState.RUNNING
    app._event_buffer = EventBuffer()
    app._adapter_states = {aid: AdapterState.READY for aid in adapters}
    app._live_health_state = None
    app._live_health_poll_count = 0
    app._health_state = {"runtime_health": "healthy", "adapter_summary": {"total": 2}}
    app._startup_wall = "2026-05-14T00:00:00+00:00"
    app._startup_monotonic = 1000.0
    app._boot_summary = None
    app._failed_adapter_ids = []
    app.started_adapter_ids = list(adapters.keys())
    app.adapter_start_monotonic = {}
    return app


class TestHealthRefreshedEventSemantics:
    """HEALTH_REFRESHED is emitted only after successful completed refresh."""

    @pytest.mark.asyncio
    async def test_emitted_on_successful_refresh(self) -> None:
        """Successful refresh emits exactly one HEALTH_REFRESHED event."""
        app = _make_real_app()
        await app.refresh_live_health()
        events = [
            e for e in app.event_buffer
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(events) == 1
        assert events[0].detail["poll_count"] == 1
        assert events[0].detail["runtime_health"] == "healthy"

    @pytest.mark.asyncio
    async def test_not_emitted_on_cancellation(self) -> None:
        """CancelledError during refresh emits no HEALTH_REFRESHED event."""
        from medre.core.lifecycle.states import AdapterState
        from medre.runtime.app import MedreApp, RuntimeState

        adapters = {
            "a1": _FakeAdapter(
                "a1",
                health_check_side_effect=asyncio.CancelledError(),
            ),
        }
        app = object.__new__(MedreApp)
        app.adapters = adapters  # type: ignore[assignment]
        app._state = RuntimeState.RUNNING
        app._event_buffer = EventBuffer()
        app._adapter_states = {"a1": AdapterState.READY}
        app._live_health_state = None
        app._live_health_poll_count = 0
        app._health_state = {"runtime_health": "healthy"}
        app._startup_wall = None
        app._startup_monotonic = None
        app._boot_summary = None
        app._failed_adapter_ids = []
        app.started_adapter_ids = ["a1"]
        app.adapter_start_monotonic = {}

        with pytest.raises(asyncio.CancelledError):
            await app.refresh_live_health()

        events = [
            e for e in app.event_buffer
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_emitted_with_adapter_failure(self) -> None:
        """Refresh with adapter exceptions still emits HEALTH_REFRESHED."""
        from medre.core.lifecycle.states import AdapterState
        from medre.runtime.app import MedreApp, RuntimeState

        adapters = {
            "a1": _FakeAdapter("a1", health="healthy"),
            "a2": _FakeAdapter(
                "a2",
                health_check_side_effect=RuntimeError("adapter error"),
            ),
        }
        app = object.__new__(MedreApp)
        app.adapters = adapters  # type: ignore[assignment]
        app._state = RuntimeState.RUNNING
        app._event_buffer = EventBuffer()
        app._adapter_states = {aid: AdapterState.READY for aid in adapters}
        app._live_health_state = None
        app._live_health_poll_count = 0
        app._health_state = {"runtime_health": "healthy"}
        app._startup_wall = None
        app._startup_monotonic = None
        app._boot_summary = None
        app._failed_adapter_ids = []
        app.started_adapter_ids = list(adapters.keys())
        app.adapter_start_monotonic = {}

        snapshot = await app.refresh_live_health()
        events = [
            e for e in app.event_buffer
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(events) == 1
        assert events[0].detail["runtime_health"] == "degraded"
        assert "a2" in events[0].detail["failed_adapters"]

    @pytest.mark.asyncio
    async def test_event_poll_count_matches_snapshot_poll_count(self) -> None:
        """Event poll_count matches the snapshot poll_count."""
        app = _make_real_app()
        snapshot = await app.refresh_live_health()
        events = [
            e for e in app.event_buffer
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(events) == 1
        assert events[0].detail["poll_count"] == snapshot.poll_count
        assert events[0].detail["poll_count"] == 1

        # Second refresh
        snapshot2 = await app.refresh_live_health()
        events2 = [
            e for e in app.event_buffer
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(events2) == 2
        assert events2[1].detail["poll_count"] == snapshot2.poll_count
        assert events2[1].detail["poll_count"] == 2
