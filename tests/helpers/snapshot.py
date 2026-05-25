"""Shared fake app builder for snapshot tests.

Extracted from ``tests/test_runtime_snapshot.py`` so that
``tests/test_runtime_snapshot_outbox.py`` can import it without
violating the no-cross-test-import rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Sentinel to distinguish "config not passed" from "config=None".
_UNSET = object()


# ---------------------------------------------------------------------------
# Minimal fakes for testing (no SDK imports)
# ---------------------------------------------------------------------------
class FakeRole(Enum):
    TRANSPORT = "transport"
    PRESENTATION = "presentation"
    HYBRID = "hybrid"


@dataclass
class FakeCapabilities:
    text: bool = True
    title: bool = False
    replies: str = "native"
    max_text_bytes: int | None = None


class FakeAdapter:
    """Minimal adapter-like object for snapshot testing."""

    def __init__(
        self,
        adapter_id: str = "test-adapter",
        platform: str = "test_platform",
        role: FakeRole | None = None,
        version: str = "0.1.0",
        capabilities: FakeCapabilities | None = None,
        health: str = "unknown",
    ) -> None:
        self.adapter_id = adapter_id
        self.platform = platform
        self.role = role or FakeRole.TRANSPORT
        self._version = version
        self._capabilities = capabilities or FakeCapabilities()
        self._last_health = health


@dataclass
class FakeRuntimeLimits:
    max_inflight_deliveries: int = 50
    max_inflight_replay_events: int = 25
    shutdown_drain_timeout_seconds: int = 10
    delivery_acquire_timeout_seconds: float = 2.0


@dataclass
class FakeRuntimeConfig:
    limits: Any = field(default_factory=FakeRuntimeLimits)


class FakeRouteStats:
    """Mimics RouteStats.snapshot()."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {}

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)


class FakeCapacityController:
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


class FakeReplayEngine:
    """Marker object — presence means replay is available."""

    pass


class FakeDiagnosticsCollector:
    """Mimics DiagnosticsCollector.snapshot()."""

    def __init__(self, replay_data: dict[str, Any] | None = None) -> None:
        self._replay_data = replay_data or {}

    def snapshot(self) -> dict[str, Any]:
        return {"replay": self._replay_data}


class FakeBuildFailure:
    """Mimics AdapterBuildFailure."""

    def __init__(self, adapter_id: str = "bad-adapter", error: str = "boom") -> None:
        self.adapter_id = adapter_id
        self.error = error


class FakeRuntimeState(Enum):
    INITIALIZED = "initialized"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Fake app builder
# ---------------------------------------------------------------------------
def make_fake_app(
    *,
    adapters: dict[str, Any] | None = None,
    state: Any = FakeRuntimeState.RUNNING,
    route_stats: Any = None,
    capacity_controller: Any = None,
    replay_engine: Any = None,
    config: Any = _UNSET,
    build_failures: list[Any] | None = None,
    diagnostics_collector: Any = None,
    startup_wall: str | None = None,
    startup_monotonic: float | None = None,
    health_state: Any = None,
    storage: Any = None,
) -> Any:
    """Build a fake app object for testing.

    Mirrors ``MedreApp.outbox_state`` semantics:
    - Always returns ``dict`` (never ``None``).
    - Uses ``_outbox_storage_authoritative`` flag for storage-vs-worker
      precedence.
    - Falls back to ``_outbox_state`` when no worker is available.
    """

    @dataclass
    class _FakeApp:
        adapters: dict[str, Any] = field(default_factory=dict)
        state: Any = FakeRuntimeState.RUNNING
        route_stats: Any = None
        _capacity_controller: Any = None
        _replay_engine: Any = None
        config: Any = field(default_factory=FakeRuntimeConfig)
        build_failures: list[Any] = field(default_factory=list)
        _diagnostics_collector: Any = None
        _startup_wall: str | None = None
        _startup_monotonic: float | None = None
        _health_state: Any = None
        _outbox_state: dict[str, int] = field(default_factory=dict)
        _outbox_storage_authoritative: bool = False
        _retry_worker: Any = None
        storage: Any = None

        async def refresh_outbox_state_from_storage(self) -> None:
            """Mirror MedreApp.refresh_outbox_state_from_storage for tests."""
            storage = getattr(self, "storage", None)
            if storage is not None:
                try:
                    self._outbox_state = await storage.count_outbox_by_status()  # type: ignore[attr-defined]
                    self._outbox_storage_authoritative = True
                except Exception:
                    pass

        @property
        def outbox_state(self) -> dict[str, int]:
            """Mirror MedreApp.outbox_state property for tests."""
            if self._outbox_storage_authoritative:
                self._outbox_storage_authoritative = False
                return dict(self._outbox_state)
            if self._retry_worker is not None:
                latest = self._retry_worker.outbox_counts
                if latest is not None:
                    self._outbox_state = dict(latest)
                    return dict(latest)
                return dict(self._outbox_state)
            return dict(self._outbox_state)

    app = _FakeApp(
        adapters=adapters or {},
        state=state,
        route_stats=route_stats,
        _capacity_controller=capacity_controller,
        _replay_engine=replay_engine,
        config=config if config is not _UNSET else FakeRuntimeConfig(),
        build_failures=build_failures or [],
        _diagnostics_collector=diagnostics_collector,
        _startup_wall=startup_wall,
        _startup_monotonic=startup_monotonic,
        _health_state=health_state,
        storage=storage,
    )
    return app
