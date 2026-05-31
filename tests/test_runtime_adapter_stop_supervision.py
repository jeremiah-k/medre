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
            storage_close_called = True
            await original_close()

        app.storage.close = _tracking_close  # type: ignore[assignment]
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert storage_close_called, "storage.close() was not called"

    @pytest.mark.asyncio
    async def test_cancelled_error_on_stop_recorded_and_others_continue(
        self, tmp_paths: MedrePaths
    ) -> None:
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)
        await app.start()

        adapter_ids = sorted(app.adapters.keys())
        cancel_id = adapter_ids[0]
        clean_id = adapter_ids[1]
        app.adapters[cancel_id] = _CancelledStopAdapter(app.adapters[cancel_id])

        with pytest.raises(RuntimeShutdownError):
            await app.stop()

        assert app.adapters[cancel_id].stop_called
        assert app.adapter_states[cancel_id] is AdapterState.FAILED
        assert app.adapter_states[clean_id] is AdapterState.STOPPED

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

        first_to_stop = adapter_ids[1]
        real_first = app.adapters[first_to_stop]._real  # type: ignore[attr-defined]
        app.adapters[first_to_stop] = _SlowStopAdapter(real_first)
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert app.adapter_states[adapter_ids[0]] is AdapterState.STOPPED

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
            storage_close_called = True
            await original_close()

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
        assert storage_close_called, "storage.close() was not called"

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
            pipeline_stop_called = True
            await original_pipeline_stop()

        app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]
        object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 0.2)

        try:
            with pytest.raises(RuntimeShutdownError):
                await app.stop()
        finally:
            object.__setattr__(app.config.runtime, "shutdown_timeout_seconds", 10)

        assert pipeline_stop_called, "pipeline_runner.stop() was not called"

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
