"""Startup containment tests for conversation projection rebuild failures."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import NoReturn

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.contracts.adapter import AdapterContext
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeStartupError


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


def _build_app(paths: MedrePaths):
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="projection-startup-test"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                )
            }
        ),
    )
    return RuntimeBuilder(config, paths).build()


async def test_projection_rebuild_failure_closes_storage_before_pipeline_start(
    tmp_paths: MedrePaths,
) -> None:
    """A failed derived-state rebuild aborts before workers or adapters start."""
    app = _build_app(tmp_paths)
    assert app.storage is not None

    storage_close_called = False
    pipeline_start_called = False
    adapter_start_called = False
    original_storage_close = app.storage.close

    async def _failing_rebuild() -> NoReturn:
        raise RuntimeError("injected conversation projection rebuild failure")

    async def _track_pipeline_start() -> None:
        nonlocal pipeline_start_called
        pipeline_start_called = True

    async def _track_storage_close() -> None:
        nonlocal storage_close_called
        storage_close_called = True
        await original_storage_close()

    adapter = app.adapters["fake_matrix"]
    original_adapter_start = adapter.start

    async def _track_adapter_start(ctx: AdapterContext) -> None:
        nonlocal adapter_start_called
        adapter_start_called = True
        await original_adapter_start(ctx)

    app.pipeline_runner.rebuild_conversation_projection = _failing_rebuild  # type: ignore[assignment]
    app.pipeline_runner.start = _track_pipeline_start  # type: ignore[assignment]
    app.storage.close = _track_storage_close  # type: ignore[assignment]
    adapter.start = _track_adapter_start  # type: ignore[method-assign]

    with pytest.raises(RuntimeStartupError, match="Failed to rebuild conversation projection"):
        await app.start()

    assert storage_close_called
    assert pipeline_start_called is False
    assert adapter_start_called is False
    assert app.state == RuntimeState.FAILED


async def test_projection_rebuild_cancellation_closes_storage_and_fails_startup(
    tmp_paths: MedrePaths,
) -> None:
    """Cancellation during projection rebuild cannot strand open storage."""
    app = _build_app(tmp_paths)
    assert app.storage is not None

    rebuild_entered = asyncio.Event()
    storage_close_called = False

    async def _initialize_storage() -> None:
        return None

    async def _blocked_rebuild() -> NoReturn:
        rebuild_entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _track_storage_close() -> None:
        nonlocal storage_close_called
        storage_close_called = True

    app.pipeline_runner.rebuild_conversation_projection = _blocked_rebuild  # type: ignore[assignment]
    app.storage.initialize = _initialize_storage  # type: ignore[assignment]
    app.storage.close = _track_storage_close  # type: ignore[assignment]

    start_task = asyncio.create_task(app.start())
    await rebuild_entered.wait()
    start_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert storage_close_called
    assert app.state is RuntimeState.FAILED
