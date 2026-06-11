"""Tests for MeshCoreAdapter diagnostics fallback and start-failure cleanup.

Covers:
- ``sdk_contact_timeout_count`` present in no-session diagnostics fallback.
- Matrix-style start-failure lifecycle cleanup: session, ctx, _started cleared.
- Post-failed-start simulate_inbound does not publish.
- _mark_started called after session.start succeeds (not before).
- stop() after failed start is idempotent (no raise).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterContext

# -- Helpers ---------------------------------------------------------------


def _make_config(**overrides: object) -> MeshCoreConfig:
    defaults: dict = {"adapter_id": "mc-startup-test"}
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_ctx(adapter_id: str = "mc-startup-test") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=__import__("logging").getLogger("test.startup"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_relay_packet(text: str = "hello") -> dict:
    return {
        "text": text,
        "pubkey_prefix": "cafe",
        "sender_timestamp": 1,
        "type": "PRIV",
        "txt_type": 0,
    }


# -- Tests -----------------------------------------------------------------


def test_fresh_diagnostics_has_sdk_contact_timeout_count_zero() -> None:
    """Diagnostics on a never-started adapter includes the fallback session
    dict with ``sdk_contact_timeout_count == 0``."""
    adapter = MeshCoreAdapter(_make_config())
    diag = adapter.diagnostics()
    assert "session" in diag
    assert diag["session"]["sdk_contact_timeout_count"] == 0


async def test_stopped_diagnostics_has_sdk_contact_timeout_count_zero() -> None:
    """After start+stop, diagnostics fallback still has
    ``sdk_contact_timeout_count == 0``."""
    adapter = MeshCoreAdapter(_make_config(connection_type="fake"))
    await adapter.start(_make_ctx())
    await adapter.stop()
    diag = adapter.diagnostics()
    assert diag["session"]["sdk_contact_timeout_count"] == 0


async def test_session_start_raises_clears_state() -> None:
    """When session.start() raises, the adapter clears _session, _started,
    and ctx so diagnostics and subsequent calls see a clean slate."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=ConnectionError("boom"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(ConnectionError, match="boom"):
            await adapter.start(_make_ctx())

    # State must be fully cleared after failed start.
    assert adapter._session is None
    assert adapter._started is False
    assert adapter.ctx is None


async def test_post_failed_start_simulate_inbound_does_not_publish() -> None:
    """After a failed start, simulate_inbound must not publish. With
    ``ctx=None`` it raises RuntimeError; the test confirms it does not
    silently publish."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=ConnectionError("fail"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(ConnectionError):
            await adapter.start(_make_ctx())

    # ctx is None after failed start → simulate_inbound raises RuntimeError.
    with pytest.raises(RuntimeError, match="has not been started"):
        await adapter.simulate_inbound(_make_relay_packet())

    # Nothing was published.
    assert adapter._inbound_published == 0


async def test_successful_start_sets_start_time_after_session_start() -> None:
    """On successful start, _mark_started is called (sets _start_time) and
    _started is True."""
    adapter = MeshCoreAdapter(_make_config(connection_type="fake"))
    ctx = _make_ctx()
    await adapter.start(ctx)

    assert adapter._started is True
    assert adapter._start_time is not None
    assert isinstance(adapter._start_time, datetime)


async def test_stop_after_failed_start_does_not_raise() -> None:
    """stop() after a failed start is idempotent — returns without error."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=ConnectionError("fail"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(ConnectionError):
            await adapter.start(_make_ctx())

    # _started is False → stop() is a no-op, must not raise.
    await adapter.stop()


# -- _start_time lifecycle tests -------------------------------------------


async def test_stop_clears_start_time() -> None:
    """After start→stop, _start_time must be None."""
    adapter = MeshCoreAdapter(_make_config(connection_type="fake"))
    await adapter.start(_make_ctx())
    assert adapter._start_time is not None

    await adapter.stop()
    assert adapter._start_time is None


async def test_failed_fresh_start_leaves_start_time_none() -> None:
    """If session.start raises on first start, _start_time stays None."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=ConnectionError("boom"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(ConnectionError, match="boom"):
            await adapter.start(_make_ctx())

    assert adapter._start_time is None


async def test_successful_start_stop_failed_restart_leaves_start_time_none() -> None:
    """start→stop→(session.start raises) must leave _start_time as None."""
    adapter = MeshCoreAdapter(_make_config(connection_type="fake"))

    # First lifecycle: start → stop.
    await adapter.start(_make_ctx())
    assert adapter._start_time is not None
    await adapter.stop()
    assert adapter._start_time is None

    # Second lifecycle: session.start raises.
    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=RuntimeError("second fail"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(RuntimeError, match="second fail"):
            await adapter.start(_make_ctx())

    assert adapter._start_time is None


# -- CancelledError / exception-split tests --------------------------------


async def test_session_start_raises_runtime_error_preserved() -> None:
    """RuntimeError from session.start propagates (not wrapped)."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=RuntimeError("rt-err"))
        mock_inst.stop = AsyncMock()
        MockSession.return_value = mock_inst

        with pytest.raises(RuntimeError, match="rt-err"):
            await adapter.start(_make_ctx())


async def test_session_start_cancelled_error_propagates() -> None:
    """CancelledError from session.start propagates and clears fields."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=asyncio.CancelledError())
        MockSession.return_value = mock_inst

        with pytest.raises(asyncio.CancelledError):
            await adapter.start(_make_ctx())

    assert adapter._session is None
    assert adapter._started is False
    assert adapter.ctx is None
    assert adapter._start_time is None


async def test_cleanup_stop_raises_on_runtime_error_path() -> None:
    """When session.start raises RuntimeError and session.stop also raises,
    the original RuntimeError propagates (not the cleanup exception)."""
    adapter = MeshCoreAdapter(_make_config(connection_type="tcp", host="127.0.0.1"))

    with patch("medre.adapters.meshcore.adapter.MeshCoreSession") as MockSession:
        mock_inst = AsyncMock()
        mock_inst.start = AsyncMock(side_effect=RuntimeError("original"))
        mock_inst.stop = AsyncMock(side_effect=OSError("cleanup-fail"))
        MockSession.return_value = mock_inst

        with pytest.raises(RuntimeError, match="original"):
            await adapter.start(_make_ctx())

    assert adapter._session is None
    assert adapter._started is False
    assert adapter.ctx is None
    assert adapter._start_time is None
