# medre-test-tier: compatibility
"""Independent adapters progress while startup retains ownership of every task."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_two_fake_adapters,
)
from medre.config.paths import resolve


async def test_slow_first_adapter_does_not_delay_independent_peer(
    tmp_path, monkeypatch
):
    """A later adapter starts before an earlier adapter's connection settles."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    release, entered, peer_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    alpha_start = app.adapters["alpha"].start
    beta_start = app.adapters["beta"].start

    async def slow_start(ctx):
        """Model asynchronous native setup, retaining a deterministic release."""
        entered.set()
        await release.wait()
        await alpha_start(ctx)

    async def ready_start(ctx):
        """Expose the peer's actual completed adapter start."""
        await beta_start(ctx)
        peer_ready.set()

    monkeypatch.setattr(app.adapters["alpha"], "start", slow_start)
    monkeypatch.setattr(app.adapters["beta"], "start", ready_start)
    starting = asyncio.create_task(app.start())
    try:
        await entered.wait()
        assert await wait_until(
            peer_ready.is_set, timeout=0.3
        ), "An independent peer waited behind the first adapter's connection"
        assert not starting.done()
    finally:
        release.set()
        await starting
        assert app.started_adapter_ids == ["alpha", "beta"]
        await app.stop()


async def test_failed_start_cleanup_does_not_hold_back_a_healthy_peer(
    tmp_path, monkeypatch
):
    """Failure isolation includes cleanup, not only connection establishment."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    release, peer_ready = asyncio.Event(), asyncio.Event()
    beta_start = app.adapters["beta"].start

    async def fail_start(ctx):
        """Fail setup before its deliberately delayed resource cleanup."""
        raise ConnectionError("Unavailable test transport")

    async def slow_stop(timeout=5):
        """Keep failed setup cleanup open while the peer starts."""
        await release.wait()

    async def ready_start(ctx):
        """Mark actual readiness rather than merely scheduling a task."""
        await beta_start(ctx)
        peer_ready.set()

    monkeypatch.setattr(app.adapters["alpha"], "start", fail_start)
    monkeypatch.setattr(app.adapters["alpha"], "stop", slow_stop)
    monkeypatch.setattr(app.adapters["beta"], "start", ready_start)
    starting = asyncio.create_task(app.start())
    try:
        assert await wait_until(peer_ready.is_set, timeout=1)
        assert not starting.done()
    finally:
        release.set()
        await starting
        assert app.started_adapter_ids == ["beta"]
        assert app.boot_summary is not None
        assert app.boot_summary.failed_adapter_ids == ("alpha",)
        await app.stop()


async def test_cancellation_settles_pending_and_ready_adapters_before_core_cleanup(
    tmp_path, monkeypatch
):
    """Parent cancellation cannot leave a child start task or partial adapter."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    entered, peer_ready = asyncio.Event(), asyncio.Event()
    beta_start = app.adapters["beta"].start
    stopped = {}
    for name, adapter in app.adapters.items():
        stopped[name] = AsyncMock(wraps=adapter.stop)
        monkeypatch.setattr(adapter, "stop", stopped[name])

    async def pending_start(ctx):
        """Represent an adapter that owns partial setup when cancelled."""
        entered.set()
        await asyncio.Event().wait()

    async def ready_start(ctx):
        """Allow the sibling to become operational before cancellation."""
        await beta_start(ctx)
        peer_ready.set()

    monkeypatch.setattr(app.adapters["alpha"], "start", pending_start)
    monkeypatch.setattr(app.adapters["beta"], "start", ready_start)
    starting = asyncio.create_task(app.start())
    await entered.wait()
    assert await wait_until(peer_ready.is_set, timeout=1)
    for _ in range(3):
        starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert starting.cancelling() == 3
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    for stop in stopped.values():
        stop.assert_awaited_once()
    assert not any(
        task.get_name().startswith("adapter-start:") and not task.done()
        for task in asyncio.all_tasks()
    )


async def test_self_cancelled_start_aborts_instead_of_disappearing(
    tmp_path, monkeypatch
):
    """TaskGroup's ignored child cancellation cannot manufacture readiness."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )

    async def cancel_start(ctx):
        """Simulate a provider that cancels its own startup."""
        raise asyncio.CancelledError

    monkeypatch.setattr(app.adapters["alpha"], "start", cancel_start)
    with pytest.raises(asyncio.CancelledError):
        await app.start()
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
