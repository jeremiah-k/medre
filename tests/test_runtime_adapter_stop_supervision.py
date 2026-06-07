"""Runtime adapter-stop supervision and cleanup continuation tests.

Focused split from ``test_runtime_cancellation.py`` so runtime cancellation
coverage does not grow past the CI 1500-line module limit.  These tests cover
runtime-enforced adapter stop timeouts and cancellation handling only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.core.lifecycle.states import AdapterState
from medre.runtime.app import RuntimeState
from medre.runtime.errors import RuntimeShutdownError
from tests.helpers.startup_cleanup import (
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
    _make_tracking_pipeline_stop,
    _make_tracking_storage_close,
    _set_shutdown_timeout,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


class _SlowStopAdapter:
    """Adapter double whose stop sleeps longer than the runtime timeout."""

    def __init__(self, real_adapter: Any, sleep_seconds: float = 300.0) -> None:
        self._real = real_adapter
        self._sleep_seconds = sleep_seconds
        self.stop_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        await asyncio.sleep(self._sleep_seconds)


class _CancelledStopAdapter:
    """Adapter double whose stop raises CancelledError."""

    def __init__(self, real_adapter: Any) -> None:
        self._real = real_adapter
        self.stop_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        raise asyncio.CancelledError("simulated cancel during stop")


class _CancellationResistantAdapter:
    """Adapter double whose stop() suppresses CancelledError indefinitely.

    Models a pathological adapter that catches and discards every
    ``CancelledError`` delivered to it (e.g. an SDK whose stop method
    wraps its work in ``try/except CancelledError: pass`` and retries).
    ``asyncio.wait_for`` cannot bound such an adapter because the
    cancel is consumed by the inner except block and the await never
    raises.  The polling-based ``_stop_adapter_with_deadline`` must
    bound it via ``task.done()`` polling instead.
    """

    def __init__(self, real_adapter: Any) -> None:
        self._real = real_adapter
        self._release = asyncio.Event()
        self.stop_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def release(self) -> None:
        """Allow the adapter's stop() to finally return."""
        self._release.set()

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        while not self._release.is_set():
            try:
                await self._release.wait()
            except asyncio.CancelledError:
                # Swallow cancellation — this is the pathological
                # case the polling-based hard deadline must bound.
                continue


class _OrderTrackingAdapter:
    """Adapter double that records stop call order."""

    def __init__(self, real_adapter: Any, order_list: list[str]) -> None:
        self._real = real_adapter
        self._order_list = order_list

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def stop(self, timeout: float = 10.0) -> None:
        self._order_list.append(self._real.adapter_id)
        await self._real.stop(timeout=timeout)


class TestAdapterStopTimeoutSupervision:
    """Runtime-enforced adapter-stop timeout behavior."""

    @pytest.mark.asyncio
    async def test_slow_adapter_does_not_block_other_adapter_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = sorted(app.adapters.keys())
        slow_id = adapter_ids[0]
        clean_id = adapter_ids[1]
        app.adapters[slow_id] = _SlowStopAdapter(app.adapters[slow_id])

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError):
                await app.stop()

        assert app.adapters[slow_id].stop_called
        assert app.adapter_states[slow_id] is AdapterState.FAILED
        assert app.adapter_states[clean_id] is AdapterState.STOPPED
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_slow_adapter_does_not_block_storage_close(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        slow_id = next(iter(app.adapters))
        app.adapters[slow_id] = _SlowStopAdapter(app.adapters[slow_id])

        assert app.storage is not None
        storage_close_called = _make_tracking_storage_close(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError):
                await app.stop()

        assert storage_close_called[0], "storage.close() did not complete"

    @pytest.mark.asyncio
    async def test_cancelled_error_on_started_adapter_stop_propagates(
        self, tmp_paths: MedrePaths
    ) -> None:
        """External CancelledError from a started adapter's stop() propagates
        out of app.stop() instead of being swallowed as RuntimeShutdownError."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = sorted(app.adapters.keys())
        cancel_id = adapter_ids[0]
        app.adapters[cancel_id] = _CancelledStopAdapter(app.adapters[cancel_id])

        with pytest.raises(asyncio.CancelledError):
            await app.stop()

        assert app.adapters[cancel_id].stop_called
        assert app.adapter_states[cancel_id] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_reverse_stop_order_preserved_with_timeouts(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = sorted(app.adapters.keys())
        stop_order: list[str] = []
        for adapter_id in adapter_ids:
            app.adapters[adapter_id] = _OrderTrackingAdapter(
                app.adapters[adapter_id], stop_order
            )

        # Replace the first-to-stop adapter with a slow version wrapped in
        # order tracking so stop_order still records the call.
        first_to_stop = adapter_ids[1]
        real_first = app.adapters[first_to_stop]._real  # type: ignore[attr-defined]
        app.adapters[first_to_stop] = _OrderTrackingAdapter(
            _SlowStopAdapter(real_first), stop_order
        )

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError):
                await app.stop()

        assert app.adapter_states[adapter_ids[0]] is AdapterState.STOPPED

        # Stop order must reflect reverse start order: slow adapter first,
        # then clean adapter — even though the slow one timed out.
        assert stop_order == [adapter_ids[1], adapter_ids[0]]

    @pytest.mark.asyncio
    async def test_all_adapters_timeout_storage_still_closes(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        for adapter_id in list(app.adapters.keys()):
            app.adapters[adapter_id] = _SlowStopAdapter(app.adapters[adapter_id])

        assert app.storage is not None
        storage_close_called = _make_tracking_storage_close(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError):
                await app.stop()

        assert all(
            state is AdapterState.FAILED for state in app.adapter_states.values()
        )
        assert storage_close_called[0], "storage.close() did not complete"

    @pytest.mark.asyncio
    async def test_pipeline_runner_stopped_after_adapter_timeouts(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_id = next(iter(app.adapters))
        app.adapters[adapter_id] = _SlowStopAdapter(app.adapters[adapter_id])

        pipeline_stop_called = _make_tracking_pipeline_stop(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError):
                await app.stop()

        assert pipeline_stop_called[0], "pipeline_runner.stop() did not complete"

    @pytest.mark.asyncio
    async def test_shutdown_error_includes_timeout_adapter_id(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        slow_id = next(iter(app.adapters))
        app.adapters[slow_id] = _SlowStopAdapter(app.adapters[slow_id])

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeShutdownError, match=slow_id) as exc_info:
                await app.stop()
            assert slow_id in str(exc_info.value)

    # -- STOPPING state guard --------------------------------------------------

    @pytest.mark.asyncio
    async def test_stop_returns_early_when_already_stopping(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Calling stop() when the runtime is already STOPPING returns
        immediately without re-entering the shutdown logic."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_id = next(iter(app.adapters))
        original_state = app.adapter_states[adapter_id]

        # Simulate being mid-stop by forcing state to STOPPING.
        app._set_state(RuntimeState.STOPPING)
        await app.stop()

        # stop() returned early — adapter state should NOT have changed.
        assert app.adapter_states[adapter_id] is original_state

        # Clean up: reset state so the real stop can run.
        app._set_state(RuntimeState.RUNNING)
        await app.stop()

    # -- Never-started adapter during shutdown ---------------------------------

    @pytest.mark.asyncio
    async def test_never_started_adapter_timeout_during_shutdown(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter in self.adapters but NOT in started_adapter_ids
        (never-started) that times out during stop() is marked FAILED.
        The timeout is intentionally NOT appended to errors, so stop()
        completes without raising RuntimeShutdownError."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        # Inject a never-started adapter with a slow stop.
        slow_ns = _SlowStopAdapter(
            type("Obj", (), {"adapter_id": "never_started_slow", "platform": "test"})(),
            sleep_seconds=300.0,
        )
        app.adapters["never_started_slow"] = slow_ns
        app._adapter_states["never_started_slow"] = AdapterState.INITIALIZING

        with _set_shutdown_timeout(app, 0.2):
            # Never-started adapter timeouts are intentionally not errors.
            await app.stop()

        assert slow_ns.stop_called
        assert app._adapter_states["never_started_slow"] is AdapterState.FAILED
        assert app.state == RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_never_started_adapter_cancelled_during_shutdown(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter in self.adapters but NOT in started_adapter_ids
        whose stop() raises CancelledError causes app.stop() to propagate
        the CancelledError (external cancellation must propagate)."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        cancel_ns = _CancelledStopAdapter(
            type(
                "Obj", (), {"adapter_id": "never_started_cancel", "platform": "test"}
            )(),
        )
        app.adapters["never_started_cancel"] = cancel_ns
        app._adapter_states["never_started_cancel"] = AdapterState.INITIALIZING

        with pytest.raises(asyncio.CancelledError):
            await app.stop()

        assert cancel_ns.stop_called

    @pytest.mark.asyncio
    async def test_cancellation_resistant_adapter_stop_is_bounded(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A cancellation-resistant adapter stop is bounded by the
        polling-based hard deadline.

        ``asyncio.wait_for`` cannot terminate a coroutine that
        suppresses ``CancelledError`` indefinitely — the cancel is
        consumed by the inner ``except`` block and the await never
        raises.  ``_stop_adapter_with_deadline`` uses polling instead
        and must bound such an adapter within ``2 * timeout`` seconds
        (cooperative stage + forced-cancel stage), returning
        ``("abandoned", ...)`` and leaving the adapter task referenced
        for the event loop to reclaim.
        """

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        real = app.adapters[next(iter(app.adapters))]
        resistant = _CancellationResistantAdapter(real)
        resistant_id = next(iter(app.adapters))
        app.adapters[resistant_id] = resistant
        app._adapter_states[resistant_id] = AdapterState.READY

        # Use a short timeout so the test runs quickly.  Total bound
        # is 2 * timeout (cooperative + cancel grace).
        timeout = 0.2

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with _set_shutdown_timeout(app, timeout):
            try:
                outcome, exc = await app._stop_adapter_with_deadline(
                    adapter=resistant,
                    adapter_id=resistant_id,
                    transport=resistant.platform,
                    timeout=float(timeout),
                )
            finally:
                # Unblock the adapter's stop() so the test can clean up.
                resistant.release()
                # Wait for the adapter's task to actually finish.
                await asyncio.sleep(0.01)
                # Close storage so the aiosqlite connection is not leaked
                # into subsequent tests' warnings.catch_warnings() context.
                if app.storage is not None and not app.storage._closed:
                    await app.storage.close()

        elapsed = loop.time() - t0

        assert outcome == "abandoned", (
            f"expected 'abandoned' for cancellation-resistant adapter, "
            f"got {outcome!r}"
        )
        assert isinstance(exc, TimeoutError)
        # Hard bound: 2 * timeout + small polling overhead.  Generous
        # bound to avoid flakiness on slow CI; the point is that it
        # does NOT hang forever.
        assert elapsed < (2 * timeout) + 0.5, (
            f"elapsed {elapsed:.3f}s exceeds hard bound "
            f"{2 * timeout + 0.5:.3f}s — polling did not bound the "
            f"cancellation-resistant adapter"
        )
        assert resistant.stop_called

    @pytest.mark.asyncio
    async def test_abandoned_adapter_stop_task_retained(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A cancellation-resistant adapter stop that is abandoned
        retains its still-alive task reference in
        ``_abandoned_adapter_stop_tasks`` so the event loop does not
        garbage-collect the task while it is still running.
        """
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        real = app.adapters[next(iter(app.adapters))]
        resistant = _CancellationResistantAdapter(real)
        resistant_id = next(iter(app.adapters))
        app.adapters[resistant_id] = resistant
        app._adapter_states[resistant_id] = AdapterState.READY

        timeout = 0.1

        with _set_shutdown_timeout(app, timeout):
            try:
                outcome, _ = await app._stop_adapter_with_deadline(
                    adapter=resistant,
                    adapter_id=resistant_id,
                    transport=resistant.platform,
                    timeout=float(timeout),
                )
                assert outcome == "abandoned"
                # The task must be retained while still alive.
                assert len(app._abandoned_adapter_stop_tasks) == 1
                retained_task = next(iter(app._abandoned_adapter_stop_tasks))
                assert not retained_task.done()
            finally:
                # Release the adapter so the task can finish.
                resistant.release()
                # Give the done callback a chance to run.
                for _ in range(20):
                    if not app._abandoned_adapter_stop_tasks:
                        break
                    await asyncio.sleep(0.01)
                # Close storage so the aiosqlite connection is not leaked
                # into subsequent tests' warnings.catch_warnings() context.
                if app.storage is not None and not app.storage._closed:
                    await app.storage.close()

        # After the task completes, the done callback removes it
        # from the retained set.
        assert app._abandoned_adapter_stop_tasks == set()


# ===================================================================
# _outcome_from_cancelled_task returning TimeoutError
# ===================================================================


class TestOutcomeFromCancelledTaskTimeout:
    """Verify that _outcome_from_cancelled_task returns TimeoutError
    instances instead of None for the exception field."""

    @pytest.mark.asyncio
    async def test_cancelled_task_returns_timeout_error(self) -> None:
        """When a cancelled task is passed to _outcome_from_cancelled_task,
        the exception field is a TimeoutError, not None."""
        from medre.runtime.app import _outcome_from_cancelled_task

        async def _cancel_self() -> None:
            raise asyncio.CancelledError("test")

        task = asyncio.create_task(_cancel_self())
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert task.done()
        outcome, exc = _outcome_from_cancelled_task(task)
        assert outcome == "timeout"
        assert isinstance(exc, asyncio.TimeoutError)
        assert exc.args[0] == "adapter stop timed out"

    @pytest.mark.asyncio
    async def test_finished_task_no_exception_returns_timeout_error(self) -> None:
        """When a finished task with no exception is passed to
        _outcome_from_cancelled_task, the exception field is TimeoutError."""
        from medre.runtime.app import _outcome_from_cancelled_task

        async def _noop() -> None:
            pass

        task = asyncio.create_task(_noop())
        await task

        outcome, exc = _outcome_from_cancelled_task(task)
        assert outcome == "timeout"
        assert isinstance(exc, asyncio.TimeoutError)


# ===================================================================
# Error-collection branches in stop() — lines 1225 and 1229
# ===================================================================


class _TimeoutErrorStopAdapter:
    """Adapter whose stop() raises TimeoutError within the deadline.

    _outcome_from_task maps TimeoutError to ("timeout", exc), so
    stop() enters the ``elif outcome == "timeout"`` branch and
    collects the exception via ``errors.append`` (line 1225).
    """

    def __init__(self, real_adapter: Any) -> None:
        self._real = real_adapter
        self.stop_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        raise TimeoutError("simulated timeout during stop")


class _RuntimeErrorStopAdapter:
    """Adapter whose stop() raises RuntimeError within the deadline.

    _outcome_from_task maps non-TimeoutError exceptions to
    ("error", exc), so stop() enters the ``else`` branch and collects
    the exception via ``errors.append`` (line 1229).
    """

    def __init__(self, real_adapter: Any) -> None:
        self._real = real_adapter
        self.stop_called = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        raise RuntimeError("simulated stop error")


class TestAdapterStopErrorCollection:
    """Verify that stop() collects adapter errors in the timeout and
    error branches and raises RuntimeShutdownError."""

    @pytest.mark.asyncio
    async def test_timeout_error_during_stop_collected(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter whose stop() raises TimeoutError is marked FAILED
        and the error is collected, raising RuntimeShutdownError."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_id = next(iter(app.adapters))
        real = app.adapters[adapter_id]
        error_adapter = _TimeoutErrorStopAdapter(real)
        app.adapters[adapter_id] = error_adapter

        with pytest.raises(RuntimeShutdownError, match=adapter_id):
            await app.stop()

        assert error_adapter.stop_called
        assert app.adapter_states[adapter_id] is AdapterState.FAILED

    @pytest.mark.asyncio
    async def test_runtime_error_during_stop_collected(
        self, tmp_paths: MedrePaths
    ) -> None:
        """An adapter whose stop() raises RuntimeError is marked FAILED
        and the error is collected, raising RuntimeShutdownError."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_id = next(iter(app.adapters))
        real = app.adapters[adapter_id]
        error_adapter = _RuntimeErrorStopAdapter(real)
        app.adapters[adapter_id] = error_adapter

        with pytest.raises(RuntimeShutdownError, match=adapter_id):
            await app.stop()

        assert error_adapter.stop_called
        assert app.adapter_states[adapter_id] is AdapterState.FAILED
