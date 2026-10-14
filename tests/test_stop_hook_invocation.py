"""Stop supervision contains call-time faults and accepts awaitable hooks."""

import asyncio
from types import SimpleNamespace

import pytest
from medre.config.model import RuntimeConfig, StorageConfig
from medre.config.paths import resolve
from medre.runtime.builder import RuntimeBuilder


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return RuntimeBuilder(
        RuntimeConfig(storage=StorageConfig(backend="memory")), resolve()
    ).build()


async def test_call_time_error_is_a_supervised_stop_outcome(app):
    error = TypeError("invalid stop invocation")

    def stop(**kwargs):
        raise error

    outcome, observed = await app._stop_adapter_with_deadline(
        SimpleNamespace(stop=stop), "node", "private_chat", 1
    )
    assert outcome == "error"
    assert observed is error


async def test_future_returning_stop_is_awaited(app):
    def stop(**kwargs):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    outcome, error = await app._stop_adapter_with_deadline(
        SimpleNamespace(stop=stop), "node", "private_chat", 1
    )
    assert outcome == "stopped"
    assert error is None
