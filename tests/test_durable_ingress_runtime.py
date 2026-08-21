"""Focused runtime coverage for durable-ingress wiring and diagnostics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from medre.core.ingress import IngressWorkerStopResult
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.errors import RuntimeShutdownError


def _bare_app(
    *, storage: object | None = None, pipeline_runner: object | None = None
) -> MedreApp:
    app = object.__new__(MedreApp)
    app.storage = storage
    app.pipeline_runner = pipeline_runner
    app._capacity_controller = None
    app._ingress_worker = None
    app._state = RuntimeState.INITIALIZED
    app.config = SimpleNamespace(
        limits=SimpleNamespace(shutdown_drain_timeout_seconds=7.5)
    )
    return app


async def test_durable_runtime_callbacks_delegate() -> None:
    checkpoint = object()
    admission = object()
    storage = SimpleNamespace(
        get_adapter_checkpoint=AsyncMock(return_value=checkpoint),
        put_adapter_checkpoint=AsyncMock(),
    )
    runner = SimpleNamespace(
        admit_ingress=AsyncMock(return_value=admission),
        handle_ingress=AsyncMock(),
    )
    app = _bare_app(storage=storage, pipeline_runner=runner)
    load = app._make_checkpoint_loader("matrix-main")
    commit = app._make_checkpoint_committer("matrix-main")
    admit = app._make_admit_inbound()
    event = object()

    assert await load("classic_sync") is checkpoint
    await commit("classic_sync", "s2", '{"recovered":true}')
    assert await admit(event, "recovered") is admission

    storage.get_adapter_checkpoint.assert_awaited_once_with(
        "matrix-main", "classic_sync"
    )
    storage.put_adapter_checkpoint.assert_awaited_once_with(
        "matrix-main",
        "classic_sync",
        "s2",
        metadata_json='{"recovered":true}',
    )
    runner.admit_ingress.assert_awaited_once_with(event, "recovered")


async def test_durable_runtime_callbacks_handle_missing_storage() -> None:
    app = _bare_app(storage=None, pipeline_runner=SimpleNamespace())
    load = app._make_checkpoint_loader("matrix-main")
    commit = app._make_checkpoint_committer("matrix-main")

    assert await load("classic_sync") is None
    with pytest.raises(RuntimeError, match="checkpoint persistence requires storage"):
        await commit("classic_sync", "s1", "{}")


async def test_default_publish_inbound_uses_durable_live_admission() -> None:
    admission = object()
    storage = object()
    runner = SimpleNamespace(
        admit_ingress=AsyncMock(return_value=admission),
        handle_ingress=AsyncMock(),
    )
    app = _bare_app(storage=storage, pipeline_runner=runner)
    publish = app._make_publish_inbound()
    event = object()

    assert await publish(event) is None
    runner.admit_ingress.assert_awaited_once_with(event, "live")
    runner.handle_ingress.assert_not_awaited()


async def test_default_publish_inbound_requires_durable_storage() -> None:
    runner = SimpleNamespace(
        admit_ingress=AsyncMock(),
    )
    app = _bare_app(storage=None, pipeline_runner=runner)
    publish = app._make_publish_inbound()
    event = object()

    with pytest.raises(
        RuntimeError, match="live adapter ingress requires durable storage"
    ):
        await publish(event)
    runner.admit_ingress.assert_not_awaited()


def test_diagnostic_snapshot_reports_ingress_worker_counters() -> None:
    worker = SimpleNamespace(
        running=True,
        processed=11,
        failures=2,
        lost_leases=3,
        terminal_failures=4,
        deferrals=6,
        active_event_id="evt-active",
        forced_cancellations=5,
    )
    app = _bare_app()
    app._ingress_worker = worker

    snapshot = app.diagnostic_snapshot()

    assert snapshot["durable_ingress"] == {
        "worker_running": True,
        "processed": 11,
        "failures": 2,
        "lost_leases": 3,
        "terminal_failures": 4,
        "deferrals": 6,
        "active_event_id": "evt-active",
        "forced_cancellations": 5,
    }
    assert snapshot["shutdown_drain_timeout_seconds"] == 7.5


def test_diagnostic_snapshot_defaults_ingress_counters_without_worker() -> None:
    app = _bare_app()

    assert app.diagnostic_snapshot()["durable_ingress"] == {
        "worker_running": False,
        "processed": 0,
        "failures": 0,
        "lost_leases": 0,
        "terminal_failures": 0,
        "deferrals": 0,
        "active_event_id": None,
        "forced_cancellations": 0,
    }


async def test_runtime_preserves_dependencies_when_ingress_stop_is_unfinished() -> None:
    storage = SimpleNamespace(close=AsyncMock())
    pipeline_runner = SimpleNamespace(stop=AsyncMock())
    app = _bare_app(storage=storage, pipeline_runner=pipeline_runner)
    app._state = RuntimeState.RUNNING
    app.config = SimpleNamespace(
        runtime=SimpleNamespace(name="test-runtime", shutdown_timeout_seconds=1.0),
        limits=SimpleNamespace(shutdown_drain_timeout_seconds=0.01),
    )
    app._event_buffer = SimpleNamespace(emit=lambda *_args, **_kwargs: None)
    app._replay_engine = None
    app._retry_worker = None
    app._capacity_controller = SimpleNamespace(stop_accepting=Mock())
    app._ingress_worker = SimpleNamespace(
        stop=AsyncMock(
            return_value=IngressWorkerStopResult(
                stopped=False,
                cancellation_requested=True,
                active_event_id="evt-active",
            )
        ),
        running=True,
        active_event_id="evt-active",
    )

    with pytest.raises(RuntimeShutdownError, match="remains active"):
        await app.stop()

    assert app.state is RuntimeState.FAILED
    app._capacity_controller.stop_accepting.assert_not_called()
    pipeline_runner.stop.assert_not_awaited()
    storage.close.assert_not_awaited()
