"""Tests for MeshCoreSession: lifecycle, reconnect, send, diagnostics.

All tests use fake mode (no SDK or hardware required).
"""

from __future__ import annotations

import pytest

from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
)
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="session-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_contact_packet(
    text: str = "hello",
    sender: str = "abc123",
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def _make_channel_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
) -> dict:
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": 0,
        "pubkey_prefix": "chan_sender",
    }


# ===================================================================
# Lifecycle
# ===================================================================


class TestMeshCoreSessionLifecycle:
    """Session start/stop/health transitions."""

    async def test_initial_state(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        assert session.connected is False
        assert session.reconnecting is False
        assert session.reconnect_attempts == 0
        assert session.last_message_time is None
        assert session.last_error is None
        assert session.transient_delivery_failures == 0
        assert session.permanent_delivery_failures == 0

    async def test_start_fake_mode(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        await session.start(callback)
        assert session.connected is True
        assert session.reconnecting is False

    async def test_start_is_idempotent(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        await session.start(noop)
        assert session.connected is True

    async def test_stop_fake_mode(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        await session.stop()
        assert session.connected is False

    async def test_stop_without_start_is_noop(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        await session.stop()  # should not raise
        assert session.connected is False

    async def test_repeated_start_stop(self) -> None:
        """Start/stop/start/stop cycle works correctly."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        assert session.connected is True
        await session.stop()
        assert session.connected is False

        # Start again
        await session.start(noop)
        assert session.connected is True
        await session.stop()
        assert session.connected is False

    async def test_non_fake_raises_without_sdk(self) -> None:
        """Non-fake mode raises MeshCoreConnectionError without SDK."""
        config = _make_config(connection_type="tcp", host="1.2.3.4")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        with pytest.raises(MeshCoreConnectionError):
            await session.start(noop)


# ===================================================================
# Inbound message callback
# ===================================================================


class TestMeshCoreSessionInbound:
    """Session message callback forwards payloads correctly."""

    async def test_fake_mode_callback_not_invoked_automatically(self) -> None:
        """In fake mode, no messages are received automatically."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        received: list[dict] = []

        async def callback(pkt: dict) -> None:
            received.append(pkt)

        await session.start(callback)
        # No messages should have been received.
        assert len(received) == 0


# ===================================================================
# Outbound send
# ===================================================================


class TestMeshCoreSessionSend:
    """Session send_text in fake mode."""

    async def test_send_text_fake_mode_returns_none(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        result = await session.send_text("contact1", "hello")
        assert result is None

    async def test_send_text_when_not_connected_raises(self) -> None:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")
        # Not started — not connected.
        with pytest.raises(MeshCoreSendError, match="not connected"):
            await session.send_text("contact1", "hello")

    async def test_send_text_channel(self) -> None:
        """Sending to a channel in fake mode returns None."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        result = await session.send_text("ignored", "hello", channel_index=0)
        assert result is None


# ===================================================================
# Diagnostics
# ===================================================================


class TestMeshCoreSessionDiagnostics:
    """Session diagnostics snapshot."""

    async def test_diagnostics_initial(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        assert diag["connected"] is False
        assert diag["reconnecting"] is False
        assert diag["reconnect_attempts"] == 0
        assert diag["last_message_time"] is None
        assert diag["last_error"] is None
        assert diag["transient_delivery_failures"] == 0
        assert diag["permanent_delivery_failures"] == 0
        assert diag["peer_count"] is None
        assert diag["mode"] == "fake"

    async def test_diagnostics_after_start(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        diag = session.diagnostics()
        assert diag["connected"] is True
        assert diag["mode"] == "fake"

    async def test_diagnostics_no_secrets(self) -> None:
        """Diagnostics never expose secrets or private keys."""
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        diag_str = str(diag)
        assert "private_key" not in diag_str
        assert "secret" not in diag_str
        assert "password" not in diag_str


# ===================================================================
# Reconnect
# ===================================================================


class TestMeshCoreSessionReconnect:
    """Reconnect loop diagnostics and guards."""

    async def test_stop_prevents_reconnect(self) -> None:
        """Setting _stop_requested prevents reconnect loop."""
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "test-1")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        # Simulate stop_requested
        session._stop_requested = True
        # Attempting reconnect loop should exit immediately
        await session._reconnect_loop()
        assert session.reconnect_attempts == 0

    async def test_reconnect_attempts_bounded(self) -> None:
        """Reconnect loop does not exceed max attempts."""
        import medre.adapters.meshcore.session as session_mod

        # Save original constants and set very short delays.
        orig_base = session_mod._RECONNECT_BASE_DELAY
        orig_max_delay = session_mod._RECONNECT_MAX_DELAY
        orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS

        session_mod._RECONNECT_BASE_DELAY = 0.01
        session_mod._RECONNECT_MAX_DELAY = 0.02
        session_mod._RECONNECT_MAX_ATTEMPTS = 3

        try:
            config = _make_config(connection_type="fake")
            session = MeshCoreSession(config, "test-1")

            async def noop(pkt: dict) -> None:
                pass

            await session.start(noop)
            await session.stop()

            async def _failing_connect():
                raise RuntimeError("test failure")

            session._connect_real = _failing_connect
            session._stop_requested = False

            await session._reconnect_loop()

            assert session.reconnect_attempts == 3  # max attempts
        finally:
            session_mod._RECONNECT_BASE_DELAY = orig_base
            session_mod._RECONNECT_MAX_DELAY = orig_max_delay
            session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts


# ===================================================================
# Diagnostics counter tracking
# ===================================================================


class TestMeshCoreSessionCounters:
    """Transient/permanent failure counters are tracked."""

    async def test_initial_counters_zero(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        assert session.transient_delivery_failures == 0
        assert session.permanent_delivery_failures == 0

    async def test_diagnostics_includes_counters(self) -> None:
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        diag = session.diagnostics()
        assert "transient_delivery_failures" in diag
        assert "permanent_delivery_failures" in diag
