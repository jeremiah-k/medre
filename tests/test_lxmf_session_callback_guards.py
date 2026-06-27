"""LxmfSession callback guard tests: post-stop callback guard, failed-start
cleanup, async callback exception handling, no-callback-without-loop, and
delivery-state thread-safe bridging.

Extracted from test_lxmf_session.py to keep file sizes manageable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.helpers.async_utils import wait_until

from medre.adapters.lxmf.errors import (
    LxmfConnectionError,
)
from medre.adapters.lxmf.session import (
    LxmfDeliveryState,
    LxmfSession,
)
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-test")
    defaults.update(overrides)
    # storage_path is required when connection_type is reticulum.
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    config = _make_config(**config_overrides)
    return LxmfSession(
        config=config,
        adapter_id=config.adapter_id,
    )


# ====================================================================
# Post-stop callback guard
# ====================================================================


class TestPostStopCallbackGuard:
    """Late SDK callbacks after stop() are silently dropped."""

    async def test_callback_not_invoked_after_stop(self) -> None:
        """After stop(), _on_lxmf_delivery drops the message and does not
        invoke the user callback."""
        received: list[dict[str, Any]] = []

        def callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=callback)

        # Deliver while running — should succeed.
        class FakeMsg:
            source_hash = b"\x01" * 16
            destination_hash = b"\x02" * 16
            hash = b"\x03" * 32
            timestamp = 1.0
            content = "before-stop"
            title = ""
            fields = {}
            signature_validated = True
            method = None

        session._on_lxmf_delivery(FakeMsg())
        await asyncio.sleep(0)
        assert len(received) == 1

        # Stop the session.
        await session.stop()

        # Late callback after stop — must be dropped.
        session._on_lxmf_delivery(FakeMsg())
        await asyncio.sleep(0)
        assert len(received) == 1, "Callback must not fire after stop()"

    async def test_stop_clears_callback_and_loop(self) -> None:
        """stop() nullifies _message_callback and _loop."""
        session = _make_session(connection_type="fake")
        await session.start(message_callback=lambda msg: None)

        assert session._message_callback is not None
        assert session._loop is not None

        await session.stop()

        assert session._message_callback is None
        assert session._loop is None


# ====================================================================
# Failed-start callback/loop cleanup
# ====================================================================


class TestFailedStartCallbackCleanup:
    """Failed LXMF start clears _message_callback, _loop, and diagnostics."""

    async def test_real_start_failure_clears_callback_and_loop(self) -> None:
        """Real-mode start failure clears _message_callback and _loop."""
        session = _make_session(connection_type="reticulum")

        def callback(msg: dict[str, Any]) -> None:
            pass

        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError, match="not installed"):
                await session.start(message_callback=callback)

        assert session._started is False
        assert session._message_callback is None
        assert session._loop is None

    async def test_real_start_failure_diagnostics_clean(self) -> None:
        """Diagnostics after failed start report not connected/reconnecting."""
        session = _make_session(connection_type="reticulum")

        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError):
                await session.start()

        assert session.connected is False
        assert session.router_running is False
        assert session.reconnecting is False

    async def test_partial_start_teardown_clears_sdk_objects(self) -> None:
        """When _connect_real creates SDK objects but then fails,
        _teardown_sdk clears _reticulum, _identity, _router."""
        session = _make_session(connection_type="reticulum")

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        # LXMRouter creation fails after Reticulum + Identity succeed.
        mock_lxmf.LXMRouter.side_effect = ValueError("storage_path invalid")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            with pytest.raises(LxmfConnectionError):
                await session.start()

        assert session._reticulum is None
        assert session._identity is None
        assert session._router is None
        assert session._message_callback is None
        assert session._loop is None
        assert session.connected is False


# ====================================================================
# Async callback exception handling
# ====================================================================


class TestAsyncCallbackExceptionHandling:
    """Async callback exceptions are consumed/logged, not unhandled."""

    async def test_async_callback_exception_consumed(self) -> None:
        """Async callback that raises does not crash the session."""
        received: list[dict[str, Any]] = []

        async def bad_async(msg: dict[str, Any]) -> None:
            received.append(msg)
            raise RuntimeError("async callback explosion")

        session = _make_session(connection_type="fake")
        await session.start(message_callback=bad_async)

        session.inject_inbound({"content": "trigger"})

        # Wait deterministically for the scheduled async task to run.
        await wait_until(lambda: len(received) >= 1)

        # Callback was invoked (received the message) and exception
        # was consumed by the done callback — session survives.
        assert len(received) == 1
        assert session.connected is True
        await session.stop()

    async def test_sync_callback_exception_still_caught(self) -> None:
        """Sync callback exception in inject_inbound is caught."""

        def bad_sync(msg: dict[str, Any]) -> None:
            raise RuntimeError("sync callback explosion")

        session = _make_session(connection_type="fake")
        await session.start(message_callback=bad_sync)

        # Should not raise — exception caught in inject_inbound.
        session.inject_inbound({"content": "trigger"})

        assert session.connected is True
        await session.stop()


# ====================================================================
# No callback when loop is None/not running
# ====================================================================


class TestNoCallbackWithoutLoop:
    """Inbound callbacks are never invoked directly on Reticulum threads
    when no valid asyncio loop is available."""

    async def test_no_callback_when_loop_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_on_lxmf_delivery drops callback when _loop is None."""
        received: list[dict[str, Any]] = []

        def callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=callback)

        # Simulate loop being cleared (as if start failed or stop cleared it).
        session._loop = None

        class FakeMsg:
            source_hash = b"\x01" * 16
            destination_hash = b"\x02" * 16
            hash = b"\x03" * 32
            timestamp = 1.0
            content = "dropped"
            title = ""
            fields = {}
            signature_validated = True
            method = None

        with caplog.at_level(logging.WARNING):
            session._on_lxmf_delivery(FakeMsg())

        await asyncio.sleep(0)

        # Callback must NOT have been invoked.
        assert len(received) == 0

        # Warning should be logged.
        assert any("dropping inbound callback" in r.message for r in caplog.records)

        await session.stop()

    async def test_no_callback_when_loop_not_running(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_on_lxmf_delivery drops callback when loop is not running."""
        received: list[dict[str, Any]] = []

        def callback(msg: dict[str, Any]) -> None:
            received.append(msg)

        session = _make_session(connection_type="fake")
        await session.start(message_callback=callback)

        # Replace loop with a non-running mock.
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = False
        session._loop = mock_loop

        class FakeMsg:
            source_hash = b"\x01" * 16
            destination_hash = b"\x02" * 16
            hash = b"\x03" * 32
            timestamp = 1.0
            content = "dropped"
            title = ""
            fields = {}
            signature_validated = True
            method = None

        with caplog.at_level(logging.WARNING):
            session._on_lxmf_delivery(FakeMsg())

        await asyncio.sleep(0)

        assert len(received) == 0
        assert any("dropping inbound callback" in r.message for r in caplog.records)

        await session.stop()


# ====================================================================
# Delivery state thread-safe bridging
# ====================================================================


class TestDeliveryStateBridging:
    """Delivery state updates are bridged onto the asyncio loop."""

    async def test_state_update_works_via_bridge(self) -> None:
        """State update via _on_delivery_state_update reaches tracking."""
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, _ = await session.send_text("ab" * 16, "hello")

        class _Msg:
            hash = native_id
            state = LxmfDeliveryState.SENT

        session._on_delivery_state_update(_Msg())

        # In fake mode, the loop IS running, so call_soon_threadsafe
        # schedules on the loop.  Give it a turn.
        await asyncio.sleep(0)

        delivery = session._outbound_deliveries.get(native_id)
        assert delivery is not None
        assert delivery.state == LxmfDeliveryState.SENT

        await session.stop()

    async def test_unknown_hash_ignored_via_bridge(self) -> None:
        """Unknown hash delivery state update is silently ignored."""
        session = _make_session(connection_type="fake")
        await session.start()

        class _Msg:
            hash = "nonexistent-hash"
            state = LxmfDeliveryState.DELIVERED

        session._on_delivery_state_update(_Msg())
        await asyncio.sleep(0)

        assert sum(session.delivery_state_counts().values()) == 0
        await session.stop()

    async def test_no_thread_exception_from_bridge(self) -> None:
        """State update with dead loop is dropped — no direct-apply."""
        session = _make_session(connection_type="fake")
        await session.start()

        native_id, _ = await session.send_text("ab" * 16, "hello")

        # Simulate a dead loop.
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = False
        session._loop = mock_loop

        class _Msg:
            hash = native_id
            state = LxmfDeliveryState.FAILED

        # Should not raise — update is dropped when loop is not running.
        session._on_delivery_state_update(_Msg())

        # State must NOT have changed (direct-apply was removed).
        delivery = session._outbound_deliveries.get(native_id)
        assert delivery is not None
        assert delivery.state == LxmfDeliveryState.OUTBOUND

        await session.stop()
