"""Provider drain and crypto-store cleanup ordering."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from medre.adapters.matrix.client_lifecycle import close_matrix_client
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import make_matrix_config


def _database() -> SimpleNamespace:
    state = SimpleNamespace(closed=False)
    state.is_closed = lambda: state.closed
    state.close = Mock(side_effect=lambda: setattr(state, "closed", True))
    return state


async def test_failed_provider_close_still_releases_store() -> None:
    database = _database()
    client = SimpleNamespace(
        store=SimpleNamespace(database=database),
        close=AsyncMock(side_effect=RuntimeError("close failed")),
    )
    with pytest.raises(RuntimeError, match="close failed"):
        await close_matrix_client(client)
    assert database.closed
    await close_matrix_client(SimpleNamespace(store=client.store))
    database.close.assert_called_once()


async def test_partial_login_failure_releases_crypto_store() -> None:
    database = _database()
    client = SimpleNamespace(
        logged_in=False,
        store=SimpleNamespace(database=database),
        close=AsyncMock(),
    )
    session = MatrixSession(make_matrix_config())
    session._client = client
    with pytest.raises(MatrixConnectionError, match="authenticate"):
        await session._finalize_start()
    assert database.closed
    assert session._client is None


async def test_stop_retains_store_until_resistant_provider_close_settles() -> None:
    database = _database()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def close() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            finished.set()

    client = SimpleNamespace(store=SimpleNamespace(database=database), close=close)
    session = MatrixSession(make_matrix_config())
    session._client = client
    stop_task = asyncio.create_task(session.stop(timeout=0.02))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(asyncio.shield(stop_task), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        assert session._client is None
        assert not database.closed, "store closed while provider still draining"
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await stop_task
        await asyncio.sleep(0)
    assert database.closed
