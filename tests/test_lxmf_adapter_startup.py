"""Tests for LxmfAdapter start-failure cleanup (Matrix-style).

Ensures that ``start()`` does not leak lifecycle state (``ctx``,
``_started``, ``_start_time``, ``_last_health``) when startup fails at
any point, and that ``session.stop()`` is called best-effort on partial
session startup failure.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.errors import LxmfConnectionError
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import CanonicalEvent

# -- Helpers ---------------------------------------------------------------


def _make_config(**overrides: object) -> LxmfConfig:
    """Build a valid LxmfConfig for adapter tests."""
    defaults: dict = {"adapter_id": "lxmf-1"}
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_ctx(
    adapter_id: str = "lxmf-1",
) -> tuple[list[CanonicalEvent], AdapterContext]:
    """Create an AdapterContext that collects published events."""
    import asyncio
    from datetime import datetime, timezone

    published: list[CanonicalEvent] = []

    async def _publish(event: CanonicalEvent) -> None:
        published.append(event)

    ctx = AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_publish,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )
    return published, ctx


# -- Tests -----------------------------------------------------------------


class TestStartFailureCleanup:
    """start() properly rolls back lifecycle state on failure."""

    async def test_non_fake_has_lxmf_false_clears_ctx(self) -> None:
        """HAS_LXMF=False with reticulum mode raises and clears ctx."""
        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        with patch("medre.adapters.lxmf.adapter.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError, match="lxmf/RNS not installed"):
                await adapter.start(ctx)

        assert adapter.ctx is None
        assert adapter._started is False
        assert adapter._start_time is None

    async def test_session_start_raises_clears_ctx(self) -> None:
        """session.start() failure clears ctx and _started."""
        config = _make_config()  # fake mode
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_session.stop = AsyncMock()
        adapter._session = mock_session

        with pytest.raises(LxmfConnectionError, match="connection refused"):
            await adapter.start(ctx)

        assert adapter.ctx is None
        assert adapter._started is False
        assert adapter._start_time is None

    async def test_post_failed_start_simulate_inbound_raises(self) -> None:
        """After failed start (ctx=None), simulate_inbound raises RuntimeError."""
        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        with patch("medre.adapters.lxmf.adapter.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)

        assert adapter.ctx is None

        packet = {"content": "hello", "message_id": "abc"}
        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound(packet)

    async def test_successful_start_sets_mark_started_after_session(
        self,
    ) -> None:
        """Successful start sets _mark_started, _started, start_time, ctx."""
        config = _make_config()  # fake mode
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.set_delivery_state_callback = MagicMock()
        adapter._session = mock_session

        await adapter.start(ctx)

        assert adapter._started is True
        assert adapter.ctx is ctx
        assert adapter._start_time is not None
        mock_session.set_delivery_state_callback.assert_called_once()

    async def test_stop_after_failed_start_does_not_raise(self) -> None:
        """stop() after failed start is a no-op (idempotent guard)."""
        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        with patch("medre.adapters.lxmf.adapter.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)

        # stop() should not raise even though start() failed
        await adapter.stop()

    async def test_failed_start_clears_last_health(self) -> None:
        """_last_health remains None after a failed start."""
        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        # Pre-set _last_health to verify it gets cleared on start attempt.
        adapter._last_health = "healthy"

        with patch("medre.adapters.lxmf.adapter.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)

        # _last_health was cleared by start() before the failure.
        assert adapter._last_health is None

    async def test_session_start_partial_calls_session_stop(self) -> None:
        """session.start() failure triggers best-effort session.stop()."""
        config = _make_config()  # fake mode
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock(side_effect=RuntimeError("partial setup"))
        mock_session.stop = AsyncMock()
        adapter._session = mock_session

        with pytest.raises(LxmfConnectionError, match="partial setup"):
            await adapter.start(ctx)

        # session.stop() should have been called during cleanup.
        mock_session.stop.assert_called_once_with(timeout=5.0)
        assert adapter.ctx is None
        assert adapter._started is False

    async def test_start_time_not_set_on_lxmf_connection_error(self) -> None:
        """LxmfConnectionError from session.start() leaves _start_time None."""
        config = _make_config()  # fake mode
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock(
            side_effect=LxmfConnectionError("auth failed"),
        )
        mock_session.stop = AsyncMock()
        adapter._session = mock_session

        with pytest.raises(LxmfConnectionError, match="auth failed"):
            await adapter.start(ctx)

        assert adapter._start_time is None
        assert adapter.ctx is None
        assert adapter._started is False

    async def test_successful_start_emits_started_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Successful start logs 'started' message."""
        config = _make_config()  # fake mode
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.set_delivery_state_callback = MagicMock()
        adapter._session = mock_session

        with caplog.at_level(logging.INFO):
            await adapter.start(ctx)

        started_logs = [
            r
            for r in caplog.records
            if "LxmfAdapter" in r.message and "started" in r.message
        ]
        assert len(started_logs) == 1

    async def test_failed_start_does_not_emit_started_log(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Failed start must NOT emit 'started' log."""
        config = _make_config(connection_type="reticulum")
        adapter = LxmfAdapter(config)
        _, ctx = _make_ctx()

        with (
            patch("medre.adapters.lxmf.adapter.HAS_LXMF", False),
            caplog.at_level(logging.INFO),
        ):
            with pytest.raises(LxmfConnectionError):
                await adapter.start(ctx)

        started_logs = [
            r
            for r in caplog.records
            if "LxmfAdapter" in r.message and "started" in r.message
        ]
        assert len(started_logs) == 0
