# medre-test-tier: compatibility
"""Independent adapters progress while startup retains ownership of every task."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.helpers.async_utils import wait_until
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
)
from medre.config.paths import resolve
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.runtime.errors import RuntimeStartupError


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
            peer_ready.is_set, timeout=1
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
        task.get_name().startswith("adapter-start") and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.parametrize("phase", ["start", "health_check"])
async def test_self_cancelled_start_aborts_instead_of_disappearing(
    tmp_path, monkeypatch, phase
):
    """TaskGroup's ignored child cancellation cannot manufacture readiness."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )

    async def cancel_start(*args):
        """Simulate a provider that cancels its own startup."""
        raise asyncio.CancelledError

    stops = {}
    for name, adapter in app.adapters.items():
        stops[name] = AsyncMock(wraps=adapter.stop)
        monkeypatch.setattr(adapter, "stop", stops[name])
    monkeypatch.setattr(app.adapters["alpha"], phase, cancel_start)
    with pytest.raises(RuntimeStartupError, match="cancelled"):
        await app.start()
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    for stop in stops.values():
        stop.assert_awaited_once()


async def test_simultaneous_child_failure_and_caller_cancellation_preserves_request(
    tmp_path, monkeypatch
):
    """A task-group exception cannot mask cancellation arriving in the same turn."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    original = app._start_adapter
    close = AsyncMock(wraps=app.storage.close)
    monkeypatch.setattr(app.storage, "close", close)

    async def failed_orchestration(adapter_id):
        """Deliver caller cancellation and a coordinator failure atomically."""
        if adapter_id == "alpha":
            for _ in range(3):
                starting.cancel()
            raise RuntimeError("test simultaneous child failure")
        return await original(adapter_id)

    monkeypatch.setattr(app, "_start_adapter", failed_orchestration)
    starting = asyncio.create_task(app.start())
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert starting.cancelling() == 3
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    assert not app.pipeline_runner.running
    close.assert_awaited_once()


async def test_child_failure_without_caller_cancel_is_a_startup_error(
    tmp_path, monkeypatch
):
    """TaskGroup's internal cancellation cannot impersonate an operator request."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )

    async def failed_orchestration(adapter_id):
        """Raise an infrastructure error while adapter startup is owned."""
        raise RuntimeError("test coordinator failure")

    monkeypatch.setattr(app, "_start_adapter", failed_orchestration)
    starting = asyncio.create_task(app.start())
    with pytest.raises(RuntimeStartupError, match="orchestration failed"):
        await starting
    assert starting.cancelling() == 0
    assert isinstance(starting.exception().__cause__, ExceptionGroup)
    assert app.state.value == "failed"
    assert not app.started_adapter_ids
    assert not app.pipeline_runner.running


@pytest.mark.parametrize("unavailable", [False, True])
async def test_degraded_initial_target_keeps_its_live_route(
    tmp_path, monkeypatch, unavailable
):
    """Degraded native health and unavailable probes cannot prune a live target."""
    config = replace(
        _config_with_two_fake_adapters(),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="healthy-to-recovering",
                    source_adapters=("beta",),
                    dest_adapters=("alpha",),
                ),
            )
        ),
    )
    app = _build_app(config, resolve(environ={"MEDRE_HOME": str(tmp_path)}))
    info = await app.adapters["alpha"].health_check()
    health = AsyncMock(
        side_effect=OSError("test unavailable health") if unavailable else None,
        return_value=replace(info, health="degraded"),
    )
    monkeypatch.setattr(app.adapters["alpha"], "health_check", health)
    event = SimpleNamespace(
        source_adapter="beta", event_kind="message.text", source_channel_id="room"
    )
    try:
        await app.start()
        assert app._adapter_states["alpha"].value == "degraded"
        matches = app.router.match(event)
        assert len(matches) == 1
        assert matches[0].id == "healthy-to-recovering"
        assert [target.adapter for target in matches[0].targets] == ["alpha"]
        assert app.startup_readiness.skipped == ()
    finally:
        await app.stop()


@pytest.mark.parametrize("reported_health", ["degraded", "unknown", "failed"])
async def test_recovering_adapter_has_degraded_startup_health(
    tmp_path, monkeypatch, reported_health
):
    """Returning from start is not evidence that the native transport is healthy."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    info = await app.adapters["alpha"].health_check()
    monkeypatch.setattr(
        app.adapters["alpha"],
        "health_check",
        AsyncMock(return_value=replace(info, health=reported_health)),
    )
    try:
        await app.start()
        assert app.boot_summary.runtime_health == "degraded"
        assert app.started_adapter_ids == ["alpha", "beta"]
        assert app._adapter_states["alpha"].value == "degraded"
    finally:
        await app.stop()


@pytest.mark.parametrize("raises", [False, True])
async def test_unavailable_initial_health_is_bounded_and_keeps_the_adapter(
    tmp_path, monkeypatch, raises
):
    """A diagnostic failure cannot invent health or retire a started transport."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    monkeypatch.setattr("medre.runtime.app._STARTUP_HEALTH_TIMEOUT_SECONDS", 0.01)

    async def unavailable():
        """Either fail immediately or cooperate with the diagnostic deadline."""
        if raises:
            raise OSError("test health unavailable")
        await asyncio.Event().wait()

    monkeypatch.setattr(app.adapters["alpha"], "health_check", unavailable)
    try:
        async with asyncio.timeout(1):
            await app.start()
        assert app.boot_summary.runtime_health == "degraded"
        assert app.started_adapter_ids == ["alpha", "beta"]
        assert app.boot_summary.failed_adapter_ids == ()
    finally:
        await app.stop()


async def test_stop_during_pending_start_rejects_unsafe_concurrent_teardown(
    tmp_path, monkeypatch
):
    """An active adapter start keeps its dependencies until owned rollback."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    alpha_start = app.adapters["alpha"].start

    async def blocked(ctx):
        entered.set()
        await release.wait()
        await alpha_start(ctx)

    monkeypatch.setattr(app.adapters["alpha"], "start", blocked)
    starting = asyncio.create_task(app.start())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(RuntimeError, match=r"cancel the start\(\) task"):
            await app.stop()
        assert app.state.value == "starting"
        assert not starting.done()
        # App resources must remain usable until the original owner finishes.
        release.set()
        await starting
        assert app.state.value == "running"
        await app.stop()
        assert app.state.value == "stopped"
    finally:
        release.set()
        if not starting.done():
            starting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await starting
        if app.state.value == "running":
            await app.stop()


async def test_orchestration_error_preserves_cancellation_drained_by_cleanup(
    tmp_path, monkeypatch
):
    """An external cancellation during rollback outranks the task-group error."""
    app = _build_app(
        _config_with_two_fake_adapters(),
        resolve(environ={"MEDRE_HOME": str(tmp_path)}),
    )

    async def failed_orchestration(_adapter_id):
        raise RuntimeError("test orchestrator failure")

    entered, release = asyncio.Event(), asyncio.Event()
    original_close = app.storage.close

    async def paused_close():
        """Receive actual caller cancellation inside resource rollback."""
        entered.set()
        try:
            await release.wait()
        finally:
            await original_close()

    monkeypatch.setattr(app, "_start_adapter", failed_orchestration)
    monkeypatch.setattr(app.storage, "close", paused_close)
    starting = asyncio.create_task(app.start())
    await entered.wait()
    # TaskGroup coordination runs in its own task and must not contaminate
    # the caller's cancellation depth on Python 3.11/3.12.
    baseline = starting.cancelling()
    assert baseline == 0
    for _ in range(3):
        starting.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert starting.cancelling() == baseline + 3
    assert app.state.value == "failed"
    assert not app.pipeline_runner.running
    assert app.storage._db is None


async def test_started_runtime_hands_its_event_buffer_to_the_retry_worker(
    tmp_path,
):
    """Worker construction at the preparation boundary wires the live buffer."""
    from dataclasses import replace

    from medre.config.model import RetryConfig

    config = replace(
        _config_with_one_fake_adapter(),
        retry=replace(RetryConfig(), enabled=True),
    )
    app = _build_app(config, resolve(environ={"MEDRE_HOME": str(tmp_path)}))
    await app.start()
    try:
        assert app._retry_worker is not None, "retry worker missing at runtime"
        assert app._retry_worker._event_buffer is app.event_buffer
    finally:
        await app.stop()
