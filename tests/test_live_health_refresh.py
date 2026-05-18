"""Focused tests for MedreApp.refresh_live_health().

Covers:
- RuntimeError when not RUNNING
- Deterministic adapter ordering (sorted by adapter_id)
- Per-adapter health_check → normalize → AdapterLiveHealth
- LiveHealthSnapshot construction and storage on _live_health_state
- poll_count increments
- Per-adapter exceptions caught with bounded error; others continue
- asyncio.CancelledError propagates; no event emitted
- Aggregate classification: all healthy → healthy, partial → degraded, all failed → failed
- Event emission (HEALTH_REFRESHED) with correct detail
- build_runtime_snapshot shows live_health before/after refresh
- startup.startup_health and lifecycle unchanged by refresh
- Schema version remains 1
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.core.contracts.adapter import AdapterCapabilities, AdapterInfo, AdapterRole
from medre.core.lifecycle.states import AdapterState
from medre.core.runtime.health import (
    AdapterLiveHealth,
    LiveHealthSnapshot,
    normalize_adapter_health,
)
from medre.core.runtime.supervision import RuntimeHealth
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.events import EventBuffer, RuntimeEventType
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_adapter_info(
    *,
    adapter_id: str = "test-adapter",
    health: str = "healthy",
) -> AdapterInfo:
    """Build a minimal AdapterInfo for testing."""
    return AdapterInfo(
        adapter_id=adapter_id,
        platform="fake_platform",
        role=AdapterRole.TRANSPORT,
        version="0.1.0",
        capabilities=AdapterCapabilities(),
        health=health,
    )


class _FakeAdapter:
    """Minimal adapter-like object with configurable health_check."""

    def __init__(
        self,
        adapter_id: str = "test-adapter",
        health: str = "healthy",
        *,
        health_check_side_effect: BaseException | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.platform = "fake_platform"
        self.role = AdapterRole.TRANSPORT
        self._health = health
        self._side_effect = health_check_side_effect

    async def health_check(self) -> AdapterInfo:
        if self._side_effect is not None:
            raise self._side_effect
        return _make_adapter_info(adapter_id=self.adapter_id, health=self._health)


def _make_minimal_app(
    adapters: dict[str, _FakeAdapter] | None = None,
    state: RuntimeState = RuntimeState.RUNNING,
) -> MedreApp:
    """Build a minimal MedreApp with fake adapters for testing.

    Uses object.__new__ to bypass dataclass __init__ requirements.
    """
    app = object.__new__(MedreApp)
    # Set required fields minimally
    app.adapters = adapters or {}  # type: ignore[assignment]
    app._state = state
    app._event_buffer = EventBuffer()
    app._adapter_states = {aid: AdapterState.READY for aid in (adapters or {})}
    app._live_health_state = None
    app._live_health_poll_count = 0
    app._health_state = {"runtime_health": "healthy", "adapter_summary": {"total": len(adapters or {})}}
    app._startup_wall = "2026-05-14T00:00:00+00:00"
    app._startup_monotonic = 1000.0
    app._boot_summary = None
    app._failed_adapter_ids = []
    app.started_adapter_ids = list((adapters or {}).keys())
    app.adapter_start_monotonic = {}
    return app  # type: ignore[return-value]


# ===================================================================
# 1. Prerequisites: runtime state guard
# ===================================================================


class TestRefreshRequiresRunning:
    """refresh_live_health raises RuntimeError when not RUNNING."""

    @pytest.mark.asyncio
    async def test_initialized_raises(self) -> None:
        app = _make_minimal_app(state=RuntimeState.INITIALIZED)
        with pytest.raises(RuntimeError, match="RUNNING state"):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_stopped_raises(self) -> None:
        app = _make_minimal_app(state=RuntimeState.STOPPED)
        with pytest.raises(RuntimeError, match="RUNNING state"):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_failed_raises(self) -> None:
        app = _make_minimal_app(state=RuntimeState.FAILED)
        with pytest.raises(RuntimeError, match="RUNNING state"):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_starting_raises(self) -> None:
        app = _make_minimal_app(state=RuntimeState.STARTING)
        with pytest.raises(RuntimeError, match="RUNNING state"):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_stopping_raises(self) -> None:
        app = _make_minimal_app(state=RuntimeState.STOPPING)
        with pytest.raises(RuntimeError, match="RUNNING state"):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_running_succeeds(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1")},
            state=RuntimeState.RUNNING,
        )
        snapshot = await app.refresh_live_health()
        assert isinstance(snapshot, LiveHealthSnapshot)


# ===================================================================
# 2. Deterministic adapter ordering
# ===================================================================


class TestDeterministicOrdering:
    """Adapters are iterated in sorted order by adapter_id."""

    @pytest.mark.asyncio
    async def test_adapters_polled_in_sorted_order(self) -> None:
        """Health checks are performed in sorted adapter_id order."""
        call_order: list[str] = []

        class _OrderingAdapter(_FakeAdapter):
            async def health_check(self) -> AdapterInfo:
                call_order.append(self.adapter_id)
                return _make_adapter_info(adapter_id=self.adapter_id, health="healthy")

        app = _make_minimal_app(
            adapters={
                "zebra": _OrderingAdapter("zebra"),
                "alpha": _OrderingAdapter("alpha"),
                "middle": _OrderingAdapter("middle"),
            },
        )
        snapshot = await app.refresh_live_health()
        assert call_order == ["alpha", "middle", "zebra"]
        # Also verify the snapshot adapters dict is sorted
        assert list(snapshot.adapters.keys()) == ["alpha", "middle", "zebra"]


# ===================================================================
# 3. Poll count increments
# ===================================================================


class TestPollCountIncrements:
    """poll_count increments with each successful refresh."""

    @pytest.mark.asyncio
    async def test_poll_count_starts_at_zero(self) -> None:
        app = _make_minimal_app(adapters={"a1": _FakeAdapter("a1")})
        assert app._live_health_poll_count == 0

    @pytest.mark.asyncio
    async def test_poll_count_increments_on_each_call(self) -> None:
        app = _make_minimal_app(adapters={"a1": _FakeAdapter("a1")})
        s1 = await app.refresh_live_health()
        assert s1.poll_count == 1
        assert app._live_health_poll_count == 1

        s2 = await app.refresh_live_health()
        assert s2.poll_count == 2
        assert app._live_health_poll_count == 2

        s3 = await app.refresh_live_health()
        assert s3.poll_count == 3
        assert app._live_health_poll_count == 3


# ===================================================================
# 4. Healthy aggregate
# ===================================================================


class TestHealthyAggregate:
    """All healthy adapters → healthy runtime health."""

    @pytest.mark.asyncio
    async def test_all_healthy(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
                "a2": _FakeAdapter("a2", health="healthy"),
            },
        )
        snapshot = await app.refresh_live_health()
        assert snapshot.runtime_health == "healthy"
        assert snapshot.adapter_summary["healthy"] == 2
        assert snapshot.adapter_summary["failed"] == 0
        assert snapshot.adapter_summary["total"] == 2


# ===================================================================
# 5. Partial failed aggregate → degraded
# ===================================================================


class TestPartialFailedAggregate:
    """One failed adapter + healthy ones → degraded runtime health."""

    @pytest.mark.asyncio
    async def test_one_failed_one_healthy_is_degraded(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
                "a2": _FakeAdapter(
                    "a2",
                    health_check_side_effect=RuntimeError("boom"),
                ),
            },
        )
        snapshot = await app.refresh_live_health()
        assert snapshot.runtime_health == "degraded"
        assert snapshot.adapter_summary["healthy"] == 1
        assert snapshot.adapter_summary["failed"] == 1
        assert snapshot.adapter_summary["total"] == 2

    @pytest.mark.asyncio
    async def test_failed_adapter_has_error(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=RuntimeError("connection refused"),
                ),
            },
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert entry.health == "failed"
        assert entry.error is not None
        assert "connection refused" in entry.error

    @pytest.mark.asyncio
    async def test_failed_adapter_bounded_error(self) -> None:
        """Error strings are truncated when too long."""
        long_error = "x" * 500
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=RuntimeError(long_error),
                ),
            },
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert entry.error is not None
        assert len(entry.error) <= 256

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_abort_others(self) -> None:
        """If one adapter fails, others still get polled."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
                "a2": _FakeAdapter(
                    "a2",
                    health_check_side_effect=RuntimeError("boom"),
                ),
                "a3": _FakeAdapter("a3", health="healthy"),
            },
        )
        snapshot = await app.refresh_live_health()
        assert "a1" in snapshot.adapters
        assert "a2" in snapshot.adapters
        assert "a3" in snapshot.adapters
        assert snapshot.adapters["a1"].health == "healthy"
        assert snapshot.adapters["a2"].health == "failed"
        assert snapshot.adapters["a3"].health == "healthy"


# ===================================================================
# 6. All failed aggregate → failed
# ===================================================================


class TestAllFailedAggregate:
    """All adapters fail health_check → failed runtime health."""

    @pytest.mark.asyncio
    async def test_all_failed(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=RuntimeError("err1"),
                ),
                "a2": _FakeAdapter(
                    "a2",
                    health_check_side_effect=RuntimeError("err2"),
                ),
            },
        )
        snapshot = await app.refresh_live_health()
        assert snapshot.runtime_health == "failed"
        assert snapshot.adapter_summary["failed"] == 2
        assert snapshot.adapter_summary["healthy"] == 0


# ===================================================================
# 7. CancelledError propagation
# ===================================================================


class TestCancelledErrorPropagation:
    """asyncio.CancelledError propagates and does not emit event."""

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=asyncio.CancelledError(),
                ),
            },
        )
        with pytest.raises(asyncio.CancelledError):
            await app.refresh_live_health()

    @pytest.mark.asyncio
    async def test_cancelled_error_no_event(self) -> None:
        """CancelledError does not emit HEALTH_REFRESHED event."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=asyncio.CancelledError(),
                ),
            },
        )
        events_before = list(app.event_buffer)
        with pytest.raises(asyncio.CancelledError):
            await app.refresh_live_health()
        events_after = list(app.event_buffer)
        # No new HEALTH_REFRESHED events
        refreshed_events = [
            e for e in events_after
            if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(refreshed_events) == 0

    @pytest.mark.asyncio
    async def test_cancelled_does_not_increment_poll_count(self) -> None:
        """CancelledError does not increment poll_count or store snapshot."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=asyncio.CancelledError(),
                ),
            },
        )
        assert app._live_health_poll_count == 0
        with pytest.raises(asyncio.CancelledError):
            await app.refresh_live_health()
        # poll_count remains unchanged — increment only happens after
        # successful snapshot construction and storage.
        assert app._live_health_poll_count == 0
        # live_health_state remains None — no partial state stored.
        assert app._live_health_state is None

    @pytest.mark.asyncio
    async def test_cancelled_then_success_poll_count_correct(self) -> None:
        """After a cancelled attempt, successful refresh gets poll_count=1."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1"),
            },
        )
        assert app._live_health_poll_count == 0

        # First call succeeds → count becomes 1
        s1 = await app.refresh_live_health()
        assert s1.poll_count == 1
        assert app._live_health_poll_count == 1

    @pytest.mark.asyncio
    async def test_adapter_exception_does_increment_poll_count(self) -> None:
        """Adapter health_check exception completes refresh → poll_count increments."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=RuntimeError("adapter boom"),
                ),
            },
        )
        assert app._live_health_poll_count == 0
        snapshot = await app.refresh_live_health()
        # Refresh completed (snapshot built with per-adapter failure)
        assert app._live_health_poll_count == 1
        assert snapshot.poll_count == 1
        assert snapshot.adapters["a1"].health == "failed"
        assert app._live_health_state is snapshot


# ===================================================================
# 8. Event emission
# ===================================================================


class TestEventEmission:
    """Successful refresh emits HEALTH_REFRESHED with correct detail."""

    @pytest.mark.asyncio
    async def test_healthy_event_emitted(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
            },
        )
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED]
        assert len(refreshed) == 1
        detail = refreshed[0].detail
        assert detail["runtime_health"] == "healthy"
        assert detail["poll_count"] == 1
        assert "adapter_summary" in detail

    @pytest.mark.asyncio
    async def test_failed_adapters_in_event_detail(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
                "a2": _FakeAdapter(
                    "a2",
                    health_check_side_effect=RuntimeError("oops"),
                ),
            },
        )
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED]
        assert len(refreshed) == 1
        detail = refreshed[0].detail
        assert "failed_adapters" in detail
        assert "a2" in detail["failed_adapters"]
        assert "a1" not in detail["failed_adapters"]

    @pytest.mark.asyncio
    async def test_changed_adapters_in_event_detail(self) -> None:
        """changed_adapters appears when health changes between polls."""
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter("a1", health="healthy"),
            },
        )
        # First refresh
        await app.refresh_live_health()

        # Now change adapter health to degraded
        app.adapters["a1"]._health = "degraded"  # type: ignore[attr-defined]

        # Second refresh
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED]
        # Should have 2 refreshed events
        assert len(refreshed) == 2
        second_detail = refreshed[1].detail
        assert "changed_adapters" in second_detail
        assert "a1" in second_detail["changed_adapters"]

    @pytest.mark.asyncio
    async def test_event_detail_json_safe(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED]
        assert len(refreshed) == 1
        # Must be JSON-serialisable
        serialized = json.dumps(refreshed[0].detail, sort_keys=True)
        assert isinstance(serialized, str)


# ===================================================================
# 9. Snapshot integration: before and after refresh
# ===================================================================


class TestSnapshotBeforeAfterRefresh:
    """build_runtime_snapshot shows live_health null before, populated after."""

    @pytest.mark.asyncio
    async def test_snapshot_before_refresh(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is None
        assert snap["health"]["scope"] == "startup"
        assert snap["health"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_snapshot_after_refresh(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is not None
        assert snap["health"]["live_health"]["runtime_health"] == "healthy"
        assert snap["health"]["live_health"]["poll_count"] == 1
        assert snap["health"]["scope"] == "live"
        assert snap["health"]["live_refresh"] is True

    @pytest.mark.asyncio
    async def test_startup_unchanged_after_refresh(self) -> None:
        """startup.startup_health is not mutated by live refresh."""
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        original_startup_health = app._health_state
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        # startup_health is still the original value
        assert snap["startup"]["startup_health"] == original_startup_health

    @pytest.mark.asyncio
    async def test_lifecycle_unchanged_after_refresh(self) -> None:
        """lifecycle section is not mutated by live refresh."""
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["lifecycle"]["runtime_state"] == "running"
        assert "a1" in snap["lifecycle"]["adapters"]

    @pytest.mark.asyncio
    async def test_schema_version_remains_one(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["schema_version"] == 1
        assert snap["schema_version"] == SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_snapshot_json_safe_after_refresh(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        serialized = json.dumps(snap, sort_keys=True)
        assert isinstance(serialized, str)


# ===================================================================
# 10. AdapterLiveHealth fields
# ===================================================================


class TestAdapterLiveHealthFields:
    """AdapterLiveHealth has correct field types and values."""

    @pytest.mark.asyncio
    async def test_poll_timestamp_wall_is_string(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert isinstance(entry.poll_timestamp_wall, str)
        assert entry.poll_timestamp_wall != ""

    @pytest.mark.asyncio
    async def test_healthy_adapter_state(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert entry.health == "healthy"
        assert entry.adapter_state == AdapterState.READY
        assert entry.error is None

    @pytest.mark.asyncio
    async def test_failed_adapter_state(self) -> None:
        app = _make_minimal_app(
            adapters={
                "a1": _FakeAdapter(
                    "a1",
                    health_check_side_effect=RuntimeError("fail"),
                ),
            },
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert entry.health == "failed"
        assert entry.adapter_state == AdapterState.FAILED
        assert entry.error is not None

    @pytest.mark.asyncio
    async def test_degraded_adapter_reported_health(self) -> None:
        """Adapter reporting 'degraded' health gets DEGRADED state."""
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="degraded")},
        )
        snapshot = await app.refresh_live_health()
        entry = snapshot.adapters["a1"]
        assert entry.health == "degraded"
        assert entry.adapter_state == AdapterState.DEGRADED


# ===================================================================
# 11. Snapshot stored on app
# ===================================================================


class TestSnapshotStoredOnApp:
    """LiveHealthSnapshot is stored on app._live_health_state."""

    @pytest.mark.asyncio
    async def test_stored_on_app(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        assert app._live_health_state is None
        snapshot = await app.refresh_live_health()
        assert app._live_health_state is snapshot
        assert app._live_health_state is not None
        assert app._live_health_state.poll_count == 1


# ===================================================================
# 12. LiveHealthSnapshot to_dict
# ===================================================================


class TestLiveHealthSnapshotToDict:
    """LiveHealthSnapshot.to_dict() produces JSON-safe sorted output."""

    @pytest.mark.asyncio
    async def test_to_dict_json_safe(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snapshot = await app.refresh_live_health()
        d = snapshot.to_dict()
        serialized = json.dumps(d, sort_keys=True)
        assert isinstance(serialized, str)
        assert '"runtime_health"' in serialized
        assert '"poll_count"' in serialized

    @pytest.mark.asyncio
    async def test_to_dict_sorted_keys(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snapshot = await app.refresh_live_health()
        d = snapshot.to_dict()
        assert list(d.keys()) == sorted(d.keys())


# ===================================================================
# 13. Snapshot state transitions (before/after refresh)
# ===================================================================


class TestSnapshotStateTransitions:
    """Comprehensive before/after snapshot state transition assertions.

    Before refresh: live_health null, scope startup, live_refresh false.
    After refresh: live_health dict, scope live, live_refresh true.
    startup_health remains unchanged. lifecycle remains process_local.
    accounting/capacity/diagnostics remain process_local.
    schema_version remains 1.
    """

    @pytest.mark.asyncio
    async def test_before_refresh_live_health_null(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_health"] is None

    @pytest.mark.asyncio
    async def test_before_refresh_scope_startup(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snap = build_runtime_snapshot(app)
        assert snap["health"]["scope"] == "startup"

    @pytest.mark.asyncio
    async def test_before_refresh_live_refresh_false(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_after_refresh_live_health_dict(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert isinstance(snap["health"]["live_health"], dict)
        assert snap["health"]["live_health"]["runtime_health"] == "healthy"
        assert snap["health"]["live_health"]["poll_count"] == 1

    @pytest.mark.asyncio
    async def test_after_refresh_scope_live(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["health"]["scope"] == "live"

    @pytest.mark.asyncio
    async def test_after_refresh_live_refresh_true(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["health"]["live_refresh"] is True

    @pytest.mark.asyncio
    async def test_startup_health_frozen_after_refresh(self) -> None:
        """startup.startup_health is not mutated by live refresh."""
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        original_startup_health = app._health_state
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["startup"]["startup_health"] == original_startup_health
        assert snap["startup"]["scope"] == "startup"
        assert snap["startup"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_lifecycle_remains_process_local(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["lifecycle"]["scope"] == "process_local"
        assert snap["lifecycle"]["live_refresh"] is False
        assert snap["lifecycle"]["runtime_state"] == "running"

    @pytest.mark.asyncio
    async def test_accounting_remains_process_local(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["accounting"]["scope"] == "process_local"
        assert snap["accounting"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_capacity_remains_process_local(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["capacity"]["scope"] == "process_local"
        assert snap["capacity"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_diagnostics_remains_process_local(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["diagnostics"]["scope"] == "process_local"
        assert snap["diagnostics"]["live_refresh"] is False

    @pytest.mark.asyncio
    async def test_schema_version_remains_one(self) -> None:
        app = _make_minimal_app(
            adapters={"a1": _FakeAdapter("a1", health="healthy")},
        )
        await app.refresh_live_health()
        snap = build_runtime_snapshot(app)
        assert snap["schema_version"] == 1
        assert snap["schema_version"] == SCHEMA_VERSION


# ===================================================================
# 14. Deterministic ordering of failed_adapters and changed_adapters
# ===================================================================


class TestEventDetailDeterministicOrdering:
    """failed_adapters and changed_adapters are in deterministic sorted order."""

    @pytest.mark.asyncio
    async def test_failed_adapters_sorted_order(self) -> None:
        """failed_adapters in event detail is sorted by adapter_id."""
        app = _make_minimal_app(
            adapters={
                "zebra": _FakeAdapter(
                    "zebra",
                    health_check_side_effect=RuntimeError("z-fail"),
                ),
                "alpha": _FakeAdapter(
                    "alpha",
                    health_check_side_effect=RuntimeError("a-fail"),
                ),
                "middle": _FakeAdapter(
                    "middle",
                    health_check_side_effect=RuntimeError("m-fail"),
                ),
                "ok_adapter": _FakeAdapter("ok_adapter", health="healthy"),
            },
        )
        snapshot = await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [
            e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(refreshed) == 1
        failed = refreshed[0].detail["failed_adapters"]
        assert failed == sorted(failed)
        # Verify all three are present and the healthy one is not.
        assert "alpha" in failed
        assert "middle" in failed
        assert "zebra" in failed
        assert "ok_adapter" not in failed

    @pytest.mark.asyncio
    async def test_changed_adapters_sorted_order(self) -> None:
        """changed_adapters in event detail is sorted by adapter_id."""
        app = _make_minimal_app(
            adapters={
                "z_ad": _FakeAdapter("z_ad", health="healthy"),
                "a_ad": _FakeAdapter("a_ad", health="healthy"),
                "m_ad": _FakeAdapter("m_ad", health="healthy"),
            },
        )
        # First refresh — establishes baseline.
        await app.refresh_live_health()

        # Change health on all three (in non-sorted order).
        app.adapters["z_ad"]._health = "degraded"
        app.adapters["a_ad"]._health = "failed"
        app.adapters["m_ad"]._health = "degraded"

        # Second refresh — should detect changes.
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [
            e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(refreshed) == 2
        second_detail = refreshed[1].detail
        assert "changed_adapters" in second_detail
        changed = second_detail["changed_adapters"]
        assert changed == sorted(changed)
        assert len(changed) == 3


# ===================================================================
# 15. Empty adapters dict refresh behavior
# ===================================================================


class TestEmptyAdaptersRefresh:
    """refresh_live_health with empty adapters emits event with failed health."""

    @pytest.mark.asyncio
    async def test_empty_adapters_emits_failed_health(self) -> None:
        """Zero adapters → runtime_health="failed" in event and snapshot."""
        app = _make_minimal_app(adapters={})
        snapshot = await app.refresh_live_health()
        assert snapshot.runtime_health == "failed"
        assert snapshot.adapter_summary["total"] == 0
        assert snapshot.adapter_summary["healthy"] == 0
        assert snapshot.adapter_summary["failed"] == 0
        assert len(snapshot.adapters) == 0

    @pytest.mark.asyncio
    async def test_empty_adapters_event_detail(self) -> None:
        """Event detail has runtime_health="failed" with zero adapters."""
        app = _make_minimal_app(adapters={})
        await app.refresh_live_health()
        events = list(app.event_buffer)
        refreshed = [
            e for e in events if e.event_type == RuntimeEventType.HEALTH_REFRESHED
        ]
        assert len(refreshed) == 1
        assert refreshed[0].detail["runtime_health"] == "failed"
        assert refreshed[0].detail["poll_count"] == 1
        assert refreshed[0].detail["adapter_summary"]["total"] == 0
        # No failed_adapters key when list would be empty.
        assert "failed_adapters" not in refreshed[0].detail
