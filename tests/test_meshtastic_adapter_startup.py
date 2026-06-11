"""Tests for MeshtasticAdapter start-failure cleanup.

Verifies that a failed session.start() leaves no stale state behind
and that a successful start sets _mark_started only after all
infrastructure is ready.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from tests.helpers.meshtastic import make_meshtastic_config

# -- Helpers ---------------------------------------------------------------


def _make_ctx(adapter_id: str = "mesh-1"):
    """Build a minimal AdapterContext for tests (no conftest dependency)."""
    from datetime import datetime, timezone

    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=_make_logger(adapter_id),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_logger(name: str = "mesh-1"):
    import logging

    return logging.getLogger(f"test.{name}")


# -- Tests -----------------------------------------------------------------


class TestSessionStartFailureCleanup:
    """session.start() failure clears all mutable start state."""

    async def test_session_start_raises_clears_state(self) -> None:
        """After session.start() raises, _session, ctx, _started are cleared."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("session boom")
        ):
            with pytest.raises(RuntimeError, match="session boom"):
                await adapter.start(ctx)

        assert adapter._session is None, "_session must be None after failed start"
        assert (
            adapter._started is False
        ), "_started must remain False after failed start"
        assert adapter.ctx is None, "ctx must be None after failed start"

    async def test_post_failed_start_simulate_inbound_raises(self) -> None:
        """After failed start, simulate_inbound raises RuntimeError (ctx is None)."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("no connect")
        ):
            with pytest.raises(RuntimeError):
                await adapter.start(ctx)

        with pytest.raises(RuntimeError, match="has not been started"):
            await adapter.simulate_inbound({"fromId": "!abc", "decoded": {}})

    async def test_stop_after_failed_start_does_not_raise(self) -> None:
        """Calling stop() after a failed start is a safe no-op."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(MeshtasticSession, "start", side_effect=RuntimeError("fail")):
            with pytest.raises(RuntimeError):
                await adapter.start(ctx)

        # stop() must not raise even though start never completed
        await adapter.stop()

    async def test_failed_start_clears_last_health(self) -> None:
        """_last_health remains None after a failed start."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(MeshtasticSession, "start", side_effect=RuntimeError("fail")):
            with pytest.raises(RuntimeError):
                await adapter.start(ctx)

        assert adapter._last_health is None

    async def test_failed_start_no_loop_or_drain_task(self) -> None:
        """After failed start, _loop and _drain_task are not created."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(MeshtasticSession, "start", side_effect=RuntimeError("fail")):
            with pytest.raises(RuntimeError):
                await adapter.start(ctx)

        assert adapter._loop is None, "_loop must be None after failed start"
        assert (
            adapter._drain_task is None
        ), "_drain_task must be None after failed start"


class TestSuccessfulStartState:
    """Successful start sets all state after session is up."""

    async def test_successful_start_sets_mark_started_after_session(
        self,
    ) -> None:
        """_mark_started is called and _start_time is set after session.start."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        await adapter.start(ctx)

        assert adapter._started is True
        assert (
            adapter._start_time is not None
        ), "_mark_started must set _start_time on successful start"
        assert adapter.ctx is ctx

        await adapter.stop()

    async def test_successful_start_creates_loop_and_drain_task(
        self,
    ) -> None:
        """After successful start, _loop and _drain_task are created."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        await adapter.start(ctx)

        assert adapter._loop is not None, "_loop must be set after successful start"
        assert (
            adapter._drain_task is not None
        ), "_drain_task must be created after successful start"

        await adapter.stop()


class TestStartTimeLifecycle:
    """_start_time tracks the adapter's active lifecycle."""

    async def test_successful_start_sets_start_time(self) -> None:
        """_start_time is not None after a successful start."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        await adapter.start(ctx)

        assert adapter._start_time is not None

        await adapter.stop()

    async def test_stop_clears_start_time(self) -> None:
        """stop() clears _start_time back to None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        await adapter.start(ctx)
        assert adapter._start_time is not None

        await adapter.stop()
        assert adapter._start_time is None

    async def test_successful_start_stop_failed_restart_leaves_start_time_none(
        self,
    ) -> None:
        """After start→stop→failed restart, _start_time stays None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        # First: successful start then stop
        await adapter.start(ctx)
        assert adapter._start_time is not None
        await adapter.stop()
        assert adapter._start_time is None

        # Second: make session.start raise
        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("restart boom")
        ):
            with pytest.raises(RuntimeError, match="restart boom"):
                await adapter.start(ctx)

        assert adapter._start_time is None

    async def test_failed_fresh_start_leaves_start_time_none(self) -> None:
        """_start_time is None after a failed start from fresh state."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("fresh boom")
        ):
            with pytest.raises(RuntimeError, match="fresh boom"):
                await adapter.start(ctx)

        assert adapter._start_time is None


class TestBestEffortSessionStopOnFailedStart:
    """Best-effort session.stop() is called on failed start."""

    async def test_session_start_partial_calls_session_stop(self) -> None:
        """session.stop() is called when session.start() raises."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("partial start")
        ):
            with patch.object(
                MeshtasticSession, "stop", new_callable=AsyncMock
            ) as mock_stop:
                with pytest.raises(RuntimeError, match="partial start"):
                    await adapter.start(ctx)

        mock_stop.assert_awaited_once_with(timeout=5.0)

    async def test_cleanup_stop_failure_does_not_mask_original_exception(
        self,
    ) -> None:
        """When both session.start() and session.stop() raise, the original
        start exception propagates."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=RuntimeError("start failed")
        ):
            with patch.object(
                MeshtasticSession,
                "stop",
                new_callable=AsyncMock,
                side_effect=RuntimeError("stop also failed"),
            ):
                with pytest.raises(RuntimeError, match="start failed"):
                    await adapter.start(ctx)

    async def test_session_start_cancelled_error_propagates(self) -> None:
        """CancelledError (BaseException, not Exception) propagates directly
        without cleanup — cancellation should propagate fast.

        The adapter's start() catches ``except Exception``, which does NOT
        intercept asyncio.CancelledError (a BaseException subclass).  This
        means CancelledError bypasses the best-effort cleanup path entirely
        and propagates immediately.  This is intentional: cancellation should
        not be delayed by cleanup attempts.
        """
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx()

        from medre.adapters.meshtastic.session import MeshtasticSession

        with patch.object(
            MeshtasticSession, "start", side_effect=asyncio.CancelledError()
        ):
            with patch.object(
                MeshtasticSession, "stop", new_callable=AsyncMock
            ) as mock_stop:
                with pytest.raises(asyncio.CancelledError):
                    await adapter.start(ctx)

        # session.stop() should NOT have been called because CancelledError
        # is a BaseException, not an Exception — it bypasses the except block.
        mock_stop.assert_not_awaited()
