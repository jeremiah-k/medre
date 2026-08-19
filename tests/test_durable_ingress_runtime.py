"""Focused runtime coverage for durable-ingress wiring and diagnostics."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from medre.runtime.app import MedreApp, RuntimeState


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
    runner = SimpleNamespace(admit_ingress=AsyncMock(return_value=admission))
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


def test_diagnostic_snapshot_reports_ingress_worker_counters() -> None:
    worker = SimpleNamespace(
        running=True,
        processed=11,
        failures=2,
        lost_leases=3,
        terminal_failures=4,
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
    }
