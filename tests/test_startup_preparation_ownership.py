# medre-test-tier: compatibility
"""Startup owns partial core resources and child tasks until rollback settles."""

import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from medre.cli import run_commands
from medre.config.paths import resolve
from medre.runtime.errors import RuntimeStartupError
from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_two_fake_adapters,
)


@pytest.mark.parametrize("phase", ["initialize", "projection", "outbox", "pipeline"])
async def test_cancelled_core_preparation_releases_partial_resources(
    tmp_path, monkeypatch, phase
):
    """Cancellation at each preparation await retires resources before returning."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    targets = {
        "initialize": (app.storage, "initialize"),
        "projection": (app.pipeline_runner, "rebuild_conversation_projection"),
        "outbox": (app.storage, "count_outbox_by_status"),
        "pipeline": (app.pipeline_runner, "start"),
    }
    owner, name = targets[phase]
    original = getattr(owner, name)
    entered = asyncio.Event()
    close = AsyncMock(wraps=app.storage.close)
    monkeypatch.setattr(app.storage, "close", close)

    async def paused_preparation(*args, **kwargs):
        """Allocate real resources before exposing the cancellation boundary."""
        await original(*args, **kwargs)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(owner, name, paused_preparation)
    starting = asyncio.create_task(app.start())
    await entered.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert starting.cancelling() == 1
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    assert not app._storage_initialized
    close.assert_awaited_once()


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM"])
def test_process_signal_during_startup_waits_for_owned_rollback(tmp_path, signal_name):
    """Real process signals pass through CLI handlers and retire unfinished setup."""
    script = """
import asyncio, json, signal, sys
from medre.cli import run_commands
from medre.config.paths import resolve
from tests.helpers.startup_cleanup import _build_app, _config_with_two_fake_adapters

async def main():
    app = _build_app(_config_with_two_fake_adapters(),
                     resolve(environ={"MEDRE_HOME": sys.argv[1]}))
    signum = getattr(signal, sys.argv[2])
    previous = signal.getsignal(signum)
    handlers = run_commands._RunSignals()
    handlers.install(run_commands._request_shutdown)
    async def pending_start(ctx):
        signal.raise_signal(signum)
        await asyncio.Event().wait()
    app.adapters["alpha"].start = pending_start
    try:
        started = await run_commands._start_interruptibly(app)
    finally:
        handlers.restore()
    print(json.dumps({"started": started, "state": app.state.value,
        "storage_open": app._storage_initialized,
        "adapters": app.started_adapter_ids,
        "pending_starts": [task.get_name() for task in asyncio.all_tasks()
            if task.get_name().startswith("adapter-start") and not task.done()],
        "handler_restored": signal.getsignal(signum) == previous}))

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), signal_name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "started": False,
        "state": "failed",
        "storage_open": False,
        "adapters": [],
        "pending_starts": [],
        "handler_restored": True,
    }


@pytest.mark.parametrize("phase", ["directories", "attachments", "pipeline"])
async def test_failed_core_preparation_owns_rollback(tmp_path, monkeypatch, phase):
    """Setup errors leave terminal state and release every allocated dependency."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    close = AsyncMock(wraps=app.storage.close)
    monkeypatch.setattr(app.storage, "close", close)

    def failed_setup():
        """Model a filesystem or attachment seam assembly failure."""
        raise OSError("test preparation failure")

    if phase == "pipeline":
        start = app.pipeline_runner.start

        async def failed_pipeline_start():
            """Fail after the pipeline has acquired its background task."""
            await start()
            raise RuntimeError("test partial pipeline failure")

        monkeypatch.setattr(app.pipeline_runner, "start", failed_pipeline_start)
    else:
        method = (
            "_ensure_dirs" if phase == "directories" else "_assemble_attachment_seam"
        )
        monkeypatch.setattr(app, method, failed_setup)
    with pytest.raises(RuntimeStartupError):
        await app.start()
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    assert not app._storage_initialized
    close.assert_awaited_once()


async def test_cli_shutdown_signal_rolls_back_once(tmp_path, monkeypatch):
    """The signal-driven CLI path leaves resource cleanup to its startup owner."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    entered = asyncio.Event()
    close = AsyncMock(wraps=app.storage.close)
    monkeypatch.setattr(app.storage, "close", close)
    monkeypatch.setattr(run_commands, "shutdown_requested", False)

    async def pending_start(ctx):
        """Keep the operator-facing startup open until a shutdown request."""
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.adapters["alpha"], "start", pending_start)
    running = asyncio.create_task(run_commands._start_interruptibly(app))
    await entered.wait()
    run_commands._request_shutdown(15, None)
    assert await running is False
    close.assert_awaited_once()
    assert app.state.value == "failed"
    assert not app.started_adapter_ids


async def test_cli_cancellation_awaits_startup_rollback(tmp_path, monkeypatch):
    """Cancelling the CLI cannot orphan its startup task or bypass rollback."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    alpha_stop = app.adapters["alpha"].stop
    monkeypatch.setattr(run_commands, "shutdown_requested", False)

    async def pending_start(ctx):
        """Hold native setup open until startup receives cancellation."""
        entered.set()
        await asyncio.Event().wait()

    async def paused_stop(timeout=5):
        """Expose rollback ownership independently of the caller's cancellation."""
        stopping.set()
        await release.wait()
        await alpha_stop(timeout)

    monkeypatch.setattr(app.adapters["alpha"], "start", pending_start)
    monkeypatch.setattr(app.adapters["alpha"], "stop", paused_stop)
    running = asyncio.create_task(run_commands._start_interruptibly(app))
    try:
        await entered.wait()
        running.cancel()
        assert await wait_until(stopping.is_set, timeout=1)
        assert not running.done(), "CLI returned while startup still owned resources"
        running.cancel()
        running.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert running.cancelling() == 3
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    assert not app._storage_initialized
