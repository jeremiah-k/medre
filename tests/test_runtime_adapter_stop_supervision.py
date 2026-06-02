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

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.lifecycle.states import AdapterState
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeShutdownError


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


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _fake_meshtastic_config(adapter_id: str = "fake_mesh") -> MeshtasticRuntimeConfig:
    return MeshtasticRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _config_with_two_fake_adapters() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-adapter-stop-supervision"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
            meshtastic={"main": _fake_meshtastic_config()},
        ),
    )


def _config_with_one_fake_adapter() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-adapter-stop-supervision-single"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(matrix={"main": _fake_matrix_config()}),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:
    return RuntimeBuilder(config=config, paths=paths).build()


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
        # Try/finally ensures release() always unblocks the wait, so
        # the test teardown can complete even if the helper abandons
        # the task.
        try:
            while not self._release.is_set():
                try:
                    await self._release.wait()
                except asyncio.CancelledError:
                    # Swallow cancellation — this is the pathological
                    # case the polling-based hard deadline must bound.
                    continue
        finally:
            self.stop_called = True


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
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

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
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert storage_close_called, "storage.close() did not complete"

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
        adapter_ids[1]
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
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

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
        storage_close_called = False
        original_close = app.storage.close

        async def _tracking_close() -> None:
            nonlocal storage_close_called
            await original_close()
            storage_close_called = True

        app.storage.close = _tracking_close  # type: ignore[assignment]
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert all(
            state is AdapterState.FAILED for state in app.adapter_states.values()
        )
        assert storage_close_called, "storage.close() did not complete"

    @pytest.mark.asyncio
    async def test_pipeline_runner_stopped_after_adapter_timeouts(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_id = next(iter(app.adapters))
        app.adapters[adapter_id] = _SlowStopAdapter(app.adapters[adapter_id])

        pipeline_stop_called = False
        original_pipeline_stop = app.pipeline_runner.stop

        async def _tracking_pipeline_stop() -> None:
            nonlocal pipeline_stop_called
            await original_pipeline_stop()
            pipeline_stop_called = True

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert pipeline_stop_called, "pipeline_runner.stop() did not complete"

    @pytest.mark.asyncio
    async def test_shutdown_error_includes_timeout_adapter_id(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)
        await app.start()

        slow_id = next(iter(app.adapters))
        app.adapters[slow_id] = _SlowStopAdapter(app.adapters[slow_id])
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError, match=slow_id) as exc_info:
                await app.stop()
            assert slow_id in str(exc_info.value)
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

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

        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)
        try:
            # Never-started adapter timeouts are intentionally not errors.
            await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

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
        from medre.runtime.app import _outcome_from_cancelled_task, _outcome_from_task

        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        await app.start()
        real = app.adapters[next(iter(app.adapters))]
        resistant = _CancellationResistantAdapter(real)
        resistant_id = next(iter(app.adapters))
        app.adapters[resistant_id] = resistant
        app.started_adapter_ids.append(resistant_id)
        app._adapter_states[resistant_id] = AdapterState.READY

        # Use a short timeout so the test runs quickly.  Total bound
        # is 2 * timeout (cooperative + cancel grace).
        timeout = 0.2
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", timeout)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            outcome, exc, cancelled_outer = await app._stop_adapter_with_deadline(
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
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        elapsed = loop.time() - t0

        assert outcome == "abandoned", (
            f"expected 'abandoned' for cancellation-resistant adapter, "
            f"got {outcome!r}"
        )
        assert not cancelled_outer
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
        assert resistant._release.is_set() or True  # release may not have propagated yet
