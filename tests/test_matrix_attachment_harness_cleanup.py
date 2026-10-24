# medre-test-tier: compatibility
"""Regression checks for nio stores owned by the Docker attachment harness."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.integration import test_synapse_attachment_smoke as smoke


@pytest.mark.parametrize("failing_stop", [None, "destination", "source", "runner"])
async def test_attachment_harness_closes_both_stores(
    monkeypatch: pytest.MonkeyPatch, failing_stop: str | None
) -> None:
    """Each SQLite store closes even if a shutdown step raises."""
    source_client, dest_client = object(), object()
    closed: list[object] = []
    monkeypatch.setattr(smoke, "close_nio_store", closed.append)

    def component(name: str, client: object | None = None) -> SimpleNamespace:
        stop = AsyncMock(
            side_effect=RuntimeError(name) if failing_stop == name else None
        )
        session = SimpleNamespace(_client=client) if client is not None else None
        return SimpleNamespace(_session=session, stop=stop)

    harness = object.__new__(smoke._AttachmentHarness)
    harness.dest_adapter = component("destination", dest_client)
    harness.source_adapter = component("source", source_client)
    harness.runner = component("runner")
    if failing_stop is not None:
        with pytest.raises(RuntimeError, match=failing_stop):
            await harness.stop()
    else:
        await harness.stop()
    assert closed == [dest_client, source_client]
    harness.dest_adapter.stop.assert_awaited_once()
    harness.source_adapter.stop.assert_awaited_once()
    harness.runner.stop.assert_awaited_once()


async def test_attachment_harness_captures_clients_before_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing adapters cannot hide the stores that teardown must release."""
    clients = [object(), object()]
    closed: list[object] = []
    monkeypatch.setattr(smoke, "close_nio_store", closed.append)
    harness = object.__new__(smoke._AttachmentHarness)
    for attr, client in (("dest_adapter", clients[0]), ("source_adapter", clients[1])):
        adapter = SimpleNamespace(_session=SimpleNamespace(_client=client))

        async def stop(*, owner: SimpleNamespace = adapter) -> None:
            owner._session = None

        adapter.stop = stop
        setattr(harness, attr, adapter)
    harness.runner = SimpleNamespace(stop=AsyncMock())
    await harness.stop()
    assert closed == clients


async def test_attachment_harness_continues_after_store_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-broken destination store must not strand the source store."""
    dest_client, source_client = object(), object()
    closed: list[object] = []

    def close(client: object) -> None:
        closed.append(client)
        if client is dest_client:
            raise RuntimeError("destination store already broken")

    monkeypatch.setattr(smoke, "close_nio_store", close)
    harness = object.__new__(smoke._AttachmentHarness)
    harness.dest_adapter = SimpleNamespace(
        _session=SimpleNamespace(_client=dest_client), stop=AsyncMock()
    )
    harness.source_adapter = SimpleNamespace(
        _session=SimpleNamespace(_client=source_client), stop=AsyncMock()
    )
    harness.runner = SimpleNamespace(stop=AsyncMock())
    await harness.stop()
    assert closed == [dest_client, source_client]
