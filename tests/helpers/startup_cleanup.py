"""Shared fixtures, config helpers, and adapter doubles for startup-cleanup
test modules.

Split from ``test_startup_cleanup_stop_supervision.py`` so that each themed
test file can import the common pieces without duplication.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

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
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.runtime.app import MedreApp
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Fixtures (import and use directly in each test module)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Config / build helpers
# ---------------------------------------------------------------------------


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _config_with_one_fake_adapter() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-startup-cleanup-stop-supervision"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={"main": _fake_matrix_config()},
        ),
    )


def _config_with_two_fake_adapters() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test-startup-cleanup-stop-supervision-two"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "alpha": MatrixRuntimeConfig(
                    adapter_id="alpha",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
                "beta": MatrixRuntimeConfig(
                    adapter_id="beta",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                ),
            },
        ),
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths) -> MedreApp:

    return RuntimeBuilder(config, paths).build()


# ---------------------------------------------------------------------------
# Shutdown timeout override
# ---------------------------------------------------------------------------


@contextmanager
def _set_shutdown_timeout(app: MedreApp, timeout: float) -> Iterator[None]:
    """Temporarily override *shutdown_timeout_seconds* on *app*.

    Stores the original value, sets *timeout*, yields, then restores the
    original — even if the body raises.

    Uses ``object.__setattr__`` because ``RuntimeOptions`` is a frozen
    dataclass; direct attribute assignment raises ``FrozenInstanceError``.
    """
    opts = app.config.runtime
    original = opts.shutdown_timeout_seconds
    object.__setattr__(opts, "shutdown_timeout_seconds", timeout)
    try:
        yield
    finally:
        object.__setattr__(opts, "shutdown_timeout_seconds", original)


# ---------------------------------------------------------------------------
# Tracking wrappers (reusable across test modules)
# ---------------------------------------------------------------------------


def _make_tracking_storage_close(app: MedreApp) -> list[bool]:
    """Wrap ``app.storage.close`` with a tracking flag.

    Returns a mutable single-element list ``[called]``.  The flag is set
    to ``True`` **before** awaiting the original close so that callers
    can verify "cleanup was entered" even if the underlying close raises
    or hangs.

    Usage::

        storage_called = _make_tracking_storage_close(app)
        # ... run test ...
        assert storage_called[0]
    """
    assert app.storage is not None
    called = [False]
    original_close = app.storage.close

    async def _tracking_close() -> None:
        called[0] = True
        await original_close()

    app.storage.close = _tracking_close  # type: ignore[assignment]
    return called


def _make_tracking_pipeline_stop(app: MedreApp) -> list[bool]:
    """Wrap ``app.pipeline_runner.stop`` with a tracking flag.

    Returns a mutable single-element list ``[called]``.  The flag is set
    to ``True`` **before** awaiting the original stop so that callers
    can verify "cleanup was entered" even if the underlying stop raises
    or hangs.
    """
    called = [False]
    original_pipeline_stop = app.pipeline_runner.stop

    async def _tracking_pipeline_stop() -> None:
        called[0] = True
        await original_pipeline_stop()

    app.pipeline_runner.stop = _tracking_pipeline_stop  # type: ignore[assignment]
    return called


# ---------------------------------------------------------------------------
# Adapter doubles
# ---------------------------------------------------------------------------


class SlowStopOnStartFailure(AdapterContract):
    """Fails on start(); stop() sleeps longer than any reasonable timeout.

    Verifies that startup cleanup uses hard-bounded polling around
    adapter.stop() to cut a hung stop short during startup failure cleanup
    so that pipeline runner stop and storage close still proceed.
    """

    adapter_id: str = "slow_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "slow_stop") -> None:
        self.adapter_id = adapter_id
        self.stop_called = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Start failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True
        await asyncio.sleep(300.0)

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


class CancelledStopOnStartFailure(AdapterContract):
    """Fails on start(); stop() raises CancelledError.

    Verifies that CancelledError during startup cleanup stop is caught
    and does not prevent pipeline/storage cleanup.
    """

    adapter_id: str = "cancelled_stop"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "cancelled_stop") -> None:
        self.adapter_id = adapter_id
        self.stop_called = False

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Start failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        self.stop_called = True
        raise asyncio.CancelledError("simulated cancel during startup cleanup stop")

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


class FailingAdapter(AdapterContract):
    """Adapter that raises on start()."""

    adapter_id: str = "failing_adapter"
    platform: str = "test"
    role: AdapterRole = AdapterRole.TRANSPORT

    def __init__(self, adapter_id: str = "failing_adapter") -> None:
        self.adapter_id = adapter_id

    async def start(self, ctx: AdapterContext) -> None:
        raise RuntimeError(f"Simulated adapter failure: {self.adapter_id}")

    async def stop(self, timeout: float = 5.0) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.0",
            capabilities=AdapterCapabilities(),
            health="failed",
        )

    async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
        return None


class SlowStopDouble:
    """Minimal adapter double whose stop() sleeps past any reasonable timeout."""

    def __init__(self, adapter_id: str = "slow", sleep_seconds: float = 300.0) -> None:
        self.adapter_id = adapter_id
        self.platform = "test"
        self.stop_called = False
        self._sleep = sleep_seconds

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        await asyncio.sleep(self._sleep)


class CancelledStopDouble:
    """Minimal adapter double whose stop() raises CancelledError."""

    def __init__(self, adapter_id: str = "cancelled") -> None:
        self.adapter_id = adapter_id
        self.platform = "test"
        self.stop_called = False

    async def stop(self, timeout: float = 10.0) -> None:
        self.stop_called = True
        raise asyncio.CancelledError("simulated cancel")


# ---------------------------------------------------------------------------
# Retry worker stub factory
# ---------------------------------------------------------------------------


def _make_cancel_retry_worker(
    message: str = "simulated cancel from retry worker stop",
    slow: bool = False,
) -> MagicMock:
    """Create a mock RetryWorker whose ``stop()`` raises CancelledError.

    If *slow* is True the stop coroutine sleeps briefly before raising,
    giving external code a window to call ``task.cancel()``.
    """
    from medre.runtime.retry import RetryWorker, RetryWorkerState

    worker_state = RetryWorkerState()
    worker = MagicMock(spec=RetryWorker)
    worker.state = worker_state

    if slow:

        async def _slow_cancel() -> None:
            await asyncio.sleep(0.1)
            raise asyncio.CancelledError(message)

        worker.stop = _slow_cancel
    else:

        async def _cancel_on_stop() -> None:
            raise asyncio.CancelledError(message)

        worker.stop = _cancel_on_stop

    return worker
