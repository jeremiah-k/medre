"""Runtime snapshot ceiling counters are integers and default to zero.

The version-1 snapshot contract carries the ``ceiling_enforced``,
``ceiling_sweep_errors`` and ``ceiling_sweep_runs`` retry keys; even a
build-only snapshot emits them, so their default values and types are part
of the stable shape alongside the deadline counters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from medre.runtime.snapshot import build_runtime_snapshot

_CEILING_COUNTER_KEYS = (
    "ceiling_enforced",
    "ceiling_sweep_errors",
    "ceiling_sweep_runs",
)


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
class _FakeApp:
    adapters: dict[str, Any] = field(default_factory=dict)
    state: Any = "running"
    route_stats: Any = None
    _capacity_controller: Any = None
    _replay_engine: Any = None
    config: Any = field(default_factory=_FakeRuntimeConfig)
    build_failures: list[Any] = field(default_factory=list)
    _diagnostics_collector: Any = None
    diagnostician: Any = None
    _startup_wall: str | None = None
    _startup_monotonic: float | None = None
    _health_state: Any = None
    _runtime_accounting: Any = None
    _boot_summary: Any = None
    retry_state: Any = None


def test_ceiling_counters_default_to_zero_integers() -> None:
    retry = build_runtime_snapshot(_FakeApp())["retry"]
    for key in _CEILING_COUNTER_KEYS:
        assert isinstance(retry[key], int)
        assert retry[key] == 0


def test_ceiling_counters_propagate_retry_state_values() -> None:
    from types import SimpleNamespace

    state = SimpleNamespace(
        abandoned=False,
        ceiling_enforced=2,
        ceiling_sweep_errors=1,
        ceiling_sweep_runs=3,
        dead_lettered=0,
        deadline_retired=0,
        deadline_sweep_errors=0,
        deadline_sweep_runs=0,
        enabled=True,
        failed=0,
        last_run_at=None,
        previous_run_in_progress=None,
        processed=0,
        running=False,
        succeeded=0,
    )
    retry = build_runtime_snapshot(_FakeApp(retry_state=state))["retry"]
    for key, expected in (
        ("ceiling_enforced", 2),
        ("ceiling_sweep_errors", 1),
        ("ceiling_sweep_runs", 3),
    ):
        assert retry[key] == expected
        assert isinstance(retry[key], int)
