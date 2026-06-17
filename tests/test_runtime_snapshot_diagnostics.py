"""Tests for runtime snapshot diagnostics surfaces.

Split from ``test_runtime_snapshot.py`` to stay within the 1 500-line
suite boundary.

Covers:
- Per-adapter diagnostics collection under ``diagnostics.adapters``.
- ``diagnostics.pipeline.running`` reflection of PipelineRunner state.
- ``snapshot_scope`` parameter and top-level field.
- Secret sanitisation within adapter diagnostics.
- Transport-specific key preservation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest

from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

# ---------------------------------------------------------------------------
# Minimal fakes (mirrors test_runtime_snapshot.py conventions)
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


class _FakeRuntimeConfig:
    limits: Any = None


class _FakeRuntimeState(Enum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# Sentinel to distinguish "config not passed" from "config=None".
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
    )
    return app


# ---------------------------------------------------------------------------
# Adapter fakes for diagnostics testing
# ---------------------------------------------------------------------------


class _FakeAdapterWithDiagnostics:
    """Adapter that exposes a synchronous diagnostics() method."""

    def __init__(
        self,
        adapter_id: str = "diag-adapter",
        platform: str = "test_platform",
        diagnostics_data: dict[str, Any] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.platform = platform
        self.role = _FakeRole.TRANSPORT
        self._version = "0.1.0"
        self._capabilities = _FakeCapabilities()
        self._last_health = "healthy"
        self._diagnostics_data = diagnostics_data

    def diagnostics(self) -> dict[str, Any]:
        if self._diagnostics_data is not None:
            return dict(self._diagnostics_data)
        return {"connected": True, "health": "healthy"}


class _FakeAdapterWithBrokenDiagnostics:
    """Adapter whose diagnostics() raises."""

    def __init__(self, adapter_id: str = "broken-diag") -> None:
        self.adapter_id = adapter_id
        self.platform = "test_platform"
        self.role = _FakeRole.TRANSPORT
        self._version = "0.1.0"
        self._capabilities = _FakeCapabilities()
        self._last_health = "unknown"

    def diagnostics(self) -> dict[str, Any]:
        raise RuntimeError("diagnostics engine exploded")


class _FakeAdapterNoDiagnostics:
    """Adapter without diagnostics() method."""

    def __init__(self, adapter_id: str = "no-diag") -> None:
        self.adapter_id = adapter_id
        self.platform = "test_platform"
        self.role = _FakeRole.TRANSPORT
        self._version = "0.1.0"
        self._capabilities = _FakeCapabilities()
        self._last_health = "unknown"


class _FakePipelineRunner:
    """Mimics PipelineRunner with a running property."""

    def __init__(self, running: bool = True) -> None:
        self._running = running

    @property
    def running(self) -> bool:
        return self._running


# ===================================================================
# Tests: Per-adapter diagnostics
# ===================================================================


class TestAdapterDiagnostics:
    """Per-adapter diagnostics are collected under diagnostics.adapters."""

    def test_adapter_diagnostics_present(self) -> None:
        """Adapter with diagnostics() gets its output under diagnostics.adapters."""
        adapter = _FakeAdapterWithDiagnostics(
            adapter_id="mesh-1",
            diagnostics_data={
                "connected": True,
                "health": "healthy",
                "queue_depth": 5,
                "outbound_mode": "long_fast",
            },
        )
        app = _make_fake_app(adapters={"mesh-1": adapter})
        snap = build_runtime_snapshot(app)

        assert "mesh-1" in snap["diagnostics"]["adapters"]
        adapter_diag = snap["diagnostics"]["adapters"]["mesh-1"]
        assert adapter_diag["connected"] is True
        assert adapter_diag["health"] == "healthy"
        # transport_specific preserves non-common keys
        assert "queue_depth" in adapter_diag.get("transport_specific", {})
        assert adapter_diag["transport_specific"]["queue_depth"] == 5
        assert "outbound_mode" in adapter_diag.get("transport_specific", {})
        assert adapter_diag["transport_specific"]["outbound_mode"] == "long_fast"

    def test_adapter_diagnostics_secrets_sanitized(self) -> None:
        """Adapter diagnostics strip secret keys."""
        adapter = _FakeAdapterWithDiagnostics(
            adapter_id="secret-1",
            diagnostics_data={
                "connected": True,
                "api_key": "sk_live_super_secret",
                "password": "hunter2",
                "access_token": "syt_abc123",
                "normal_metric": 42,
            },
        )
        app = _make_fake_app(adapters={"secret-1": adapter})
        snap = build_runtime_snapshot(app)

        adapter_diag = snap["diagnostics"]["adapters"]["secret-1"]
        serialized = json.dumps(adapter_diag).lower()
        # Secret keys should not appear in the output
        assert "api_key" not in serialized
        assert "password" not in serialized
        assert "access_token" not in serialized
        # Normal data should survive
        assert adapter_diag["connected"] is True
        # normal_metric is non-common, goes to transport_specific
        assert adapter_diag["transport_specific"]["normal_metric"] == 42

    def test_adapter_diagnostics_exception_does_not_break_snapshot(self) -> None:
        """Adapter whose diagnostics() raises gets error status, not crash."""
        adapter = _FakeAdapterWithBrokenDiagnostics(adapter_id="boom-1")
        app = _make_fake_app(adapters={"boom-1": adapter})
        snap = build_runtime_snapshot(app)

        # Snapshot should still be valid
        assert "schema_version" in snap
        assert "boom-1" in snap["diagnostics"]["adapters"]
        adapter_diag = snap["diagnostics"]["adapters"]["boom-1"]
        assert adapter_diag["status"] == "diagnostics_error"
        assert "error" in adapter_diag
        assert (
            "RuntimeError" in adapter_diag["error"]
            or "diagnostics engine" in adapter_diag["error"]
        )

    def test_adapter_without_diagnostics_omitted(self) -> None:
        """Adapter without diagnostics() is not in diagnostics.adapters."""
        adapter = _FakeAdapterNoDiagnostics(adapter_id="plain-1")
        app = _make_fake_app(adapters={"plain-1": adapter})
        snap = build_runtime_snapshot(app)

        assert "plain-1" not in snap["diagnostics"]["adapters"]
        assert snap["diagnostics"]["adapters"] == {}

    def test_meshtastic_like_keys_survive(self) -> None:
        """Meshtastic-specific keys like queue/outbound_mode survive."""
        adapter = _FakeAdapterWithDiagnostics(
            adapter_id="mesh-radio",
            diagnostics_data={
                "connected": True,
                "health": "healthy",
                "queue_depth": 10,
                "outbound_mode": "short_fast",
                "node_count": 5,
                "channel_utilization": 0.23,
            },
        )
        app = _make_fake_app(adapters={"mesh-radio": adapter})
        snap = build_runtime_snapshot(app)

        adapter_diag = snap["diagnostics"]["adapters"]["mesh-radio"]
        specific = adapter_diag.get("transport_specific", {})
        assert specific["queue_depth"] == 10
        assert specific["outbound_mode"] == "short_fast"
        assert specific["node_count"] == 5
        assert specific["channel_utilization"] == 0.23

    def test_adapter_diagnostics_keys_sorted(self) -> None:
        """Adapter diagnostics output has sorted keys."""
        adapter = _FakeAdapterWithDiagnostics(
            adapter_id="sorted-1",
            diagnostics_data={
                "connected": True,
                "health": "healthy",
                "zulu_key": 1,
                "alpha_key": 2,
            },
        )
        app = _make_fake_app(adapters={"sorted-1": adapter})
        snap = build_runtime_snapshot(app)

        adapter_diag = snap["diagnostics"]["adapters"]["sorted-1"]
        keys = list(adapter_diag.keys())
        assert keys == sorted(keys)

    def test_diagnostics_section_has_adapters_key(self) -> None:
        """The diagnostics section has an 'adapters' key."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "adapters" in snap["diagnostics"]
        assert isinstance(snap["diagnostics"]["adapters"], dict)


# ===================================================================
# Tests: Pipeline running state
# ===================================================================


class TestPipelineRunning:
    """diagnostics.pipeline.running reflects PipelineRunner state."""

    def test_pipeline_running_true(self) -> None:
        """When pipeline_runner.running is True, diagnostics.pipeline.running is True."""
        app = _make_fake_app()
        app.pipeline_runner = _FakePipelineRunner(running=True)  # type: ignore[attr-defined]
        snap = build_runtime_snapshot(app)

        assert snap["diagnostics"]["pipeline"]["running"] is True

    def test_pipeline_running_false(self) -> None:
        """When pipeline_runner.running is False, diagnostics.pipeline.running is False."""
        app = _make_fake_app()
        app.pipeline_runner = _FakePipelineRunner(running=False)  # type: ignore[attr-defined]
        snap = build_runtime_snapshot(app)

        assert snap["diagnostics"]["pipeline"]["running"] is False

    def test_pipeline_running_null_when_absent(self) -> None:
        """When no pipeline_runner, diagnostics.pipeline.running is None."""
        app = _make_fake_app()
        snap = build_runtime_snapshot(app)

        assert snap["diagnostics"]["pipeline"]["running"] is None

    def test_pipeline_section_has_running_key(self) -> None:
        """diagnostics.pipeline is a dict with a 'running' key."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert "pipeline" in snap["diagnostics"]
        assert "running" in snap["diagnostics"]["pipeline"]


# ===================================================================
# Tests: snapshot_scope parameter
# ===================================================================


class TestSnapshotScope:
    """snapshot_scope parameter and top-level field."""

    def test_default_snapshot_scope_is_build(self) -> None:
        """Default snapshot_scope is 'build'."""
        snap = build_runtime_snapshot(_make_fake_app())
        assert snap["snapshot_scope"] == "build"

    def test_override_snapshot_scope_live(self) -> None:
        """snapshot_scope='live' is reflected in output."""
        snap = build_runtime_snapshot(_make_fake_app(), snapshot_scope="live")
        assert snap["snapshot_scope"] == "live"

    def test_snapshot_scope_in_json(self) -> None:
        """snapshot_scope appears in JSON output."""
        snap = build_runtime_snapshot(_make_fake_app(), snapshot_scope="live")
        serialized = json.dumps(snap, sort_keys=True)
        assert '"snapshot_scope": "live"' in serialized

    def test_snapshot_scope_preserves_other_sections(self) -> None:
        """Setting snapshot_scope does not disturb other sections."""
        snap = build_runtime_snapshot(_make_fake_app(), snapshot_scope="live")
        assert snap["schema_version"] == SCHEMA_VERSION
        assert snap["lifecycle"]["scope"] == "process_local"
        assert snap["startup"]["scope"] == "startup"
        assert snap["health"]["scope"] == "startup"

    def test_snapshot_scope_build_reflected(self) -> None:
        """snapshot_scope='build' is reflected in output."""
        snap = build_runtime_snapshot(_make_fake_app(), snapshot_scope="build")
        assert snap["snapshot_scope"] == "build"

    def test_invalid_snapshot_scope_raises(self) -> None:
        """Invalid snapshot_scope raises ValueError."""
        with pytest.raises(ValueError, match="snapshot_scope"):
            build_runtime_snapshot(_make_fake_app(), snapshot_scope="invalid")
