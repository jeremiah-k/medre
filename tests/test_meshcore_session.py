"""Tests for MeshCoreSession: lifecycle, reconnect, send, diagnostics.

All tests use fake mode (no SDK or hardware required).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from medre.adapters.meshcore.errors import (
    MeshCoreConnectionError,
    MeshCoreSendError,
)
from medre.adapters.meshcore.session import (
    MeshCoreSession,
    _extract_expected_ack,
)
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
        assert "peer_count" not in diag
        assert diag["device_name"] is None
        assert diag["public_key_prefix"] is None
        assert diag["radio_freq"] is None
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


# ===================================================================
# _extract_expected_ack edge cases (lines 156-166)
# ===================================================================


class TestExtractExpectedAck:
    """Cover _extract_expected_ack for non-4-byte, None, and non-bytes inputs."""

    def test_3_bytes_returns_none(self) -> None:
        """3-byte input logs warning and returns None."""
        assert _extract_expected_ack(b"abc") is None

    def test_8_bytes_returns_none(self) -> None:
        """8-byte input logs warning and returns None."""
        assert _extract_expected_ack(b"abcdefgh") is None

    def test_4_bytes_returns_hex(self) -> None:
        """4-byte input returns lowercase hex string."""
        result = _extract_expected_ack(b"\x00\x01\x02\x03")
        assert result == "00010203"

    def test_none_returns_none(self) -> None:
        """None input returns None (not bytes)."""
        assert _extract_expected_ack(None) is None

    def test_string_returns_none(self) -> None:
        """String input returns None (not bytes)."""
        assert _extract_expected_ack("not bytes") is None

    def test_empty_bytes_returns_none(self) -> None:
        """Empty bytes input returns None."""
        assert _extract_expected_ack(b"") is None


# ===================================================================
# Native ID extraction from result shapes (lines 764-795)
# ===================================================================


class TestNativeIdExtraction:
    """Cover native_id extraction from dict, .payload, .attributes result shapes.

    These tests exercise the _send_real result parsing paths.  We use TCP
    config (not fake) so send_text dispatches to _send_real, then inject a
    mock _meshcore with controlled return values.
    """

    def _make_session_with_mock(self) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with connected=True and a mock _meshcore."""
        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "nid-test")
        # Mark connected without going through start (no real SDK needed).
        session._diag.connected = True

        mock_meshcore = AsyncMock()
        mock_meshcore.commands = AsyncMock()
        mock_meshcore.commands.send_msg = AsyncMock()
        mock_meshcore.commands.send_chan_msg = AsyncMock()
        session._meshcore = mock_meshcore

        return session, mock_meshcore

    async def test_dict_result_with_expected_ack(self) -> None:
        """Dict result with expected_ack 4 bytes → hex string native_id."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        result = await session.send_text("contact1", "test")
        assert result == "01020304"

    async def test_dict_result_with_message_id_bytes_fallback(self) -> None:
        """Dict result with expected_ack=None, message_id bytes → hex fallback."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": None,
            "message_id": b"\xaa\xbb",
        }

        result = await session.send_text("contact1", "test")
        assert result == "aabb"

    async def test_dict_result_with_message_id_str_fallback(self) -> None:
        """Dict result with expected_ack=None, message_id str → str fallback."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": None,
            "message_id": "str-id",
        }

        result = await session.send_text("contact1", "test")
        assert result == "str-id"

    async def test_object_result_with_payload_dict(self) -> None:
        """Object result with .payload dict containing expected_ack → hex."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={"expected_ack": b"\x01\x02\x03\x04"},
        )

        result = await session.send_text("contact1", "test")
        assert result == "01020304"

    async def test_object_result_with_attributes_dict(self) -> None:
        """Object result with .attributes dict containing expected_ack → hex."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={},
            attributes={"expected_ack": b"\x01\x02\x03\x04"},
        )

        result = await session.send_text("contact1", "test")
        assert result == "01020304"

    async def test_object_result_none_when_no_id(self) -> None:
        """Object result with no expected_ack/message_id → native_id is None."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={},
            attributes={},
        )

        result = await session.send_text("contact1", "test")
        assert result is None


# ===================================================================
# Expected-ack persistence and JSON-safety (W1 audit closure)
# ===================================================================


class TestExpectedAckPersistence:
    """Expected_ack is ephemeral 4-byte ACK correlation persisted as hex
    string native_message_id.  Per W1 audit: NOT a durable protocol-level
    message ID — volatile, in-memory circular buffer on the firmware side."""

    def _make_session_with_mock(self) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with connected=True and a mock _meshcore."""
        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "ack-test")
        session._diag.connected = True

        mock_meshcore = AsyncMock()
        mock_meshcore.commands = AsyncMock()
        mock_meshcore.commands.send_msg = AsyncMock()
        mock_meshcore.commands.send_chan_msg = AsyncMock()
        session._meshcore = mock_meshcore

        return session, mock_meshcore

    async def test_expected_ack_hex_is_json_safe(self) -> None:
        """The hex string from _extract_expected_ack is pure ASCII —
        JSON-serializable without encoding issues."""
        import json

        raw = b"\xde\xad\xbe\xef"
        hex_id = _extract_expected_ack(raw)
        assert hex_id == "deadbeef"
        # Verify it survives JSON round-trip
        assert json.loads(json.dumps({"id": hex_id}))["id"] == "deadbeef"

    async def test_expected_ack_persisted_through_send_text(self) -> None:
        """send_text returns expected_ack hex as native_id for DMs."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        result = await session.send_text("contact1", "test")
        # The hex string IS the native_id — persisted as AdapterDeliveryResult
        # native_message_id for cross-transport correlation.
        assert result == "01020304"

    async def test_channel_send_no_expected_ack(self) -> None:
        """Channel sends return no expected_ack (per audit: no ACK protocol
        for channel messages)."""
        session, mock_mc = self._make_session_with_mock()
        # Channel send returns empty dict — no expected_ack
        mock_mc.commands.send_chan_msg.return_value = {}

        result = await session.send_text("ignored", "test", channel_index=0)
        assert result is None


# ===================================================================
# Channel send routing and ACK tracking integration
# ===================================================================


class TestChannelSendAndAckTracking:
    """Verify send_chan_msg vs send_msg dispatch and ACK extraction.

    These tests exercise the _send_real branching logic:
      - channel_index is not None  →  send_chan_msg(channel_index, text)
      - channel_index is None      →  send_msg(contact_id, text)

    Plus ACK extraction from various SDK result shapes.
    """

    def _make_session_with_mock(self) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with connected=True and a mock _meshcore."""
        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "chan-ack-test")
        session._diag.connected = True

        mock_meshcore = AsyncMock()
        mock_meshcore.commands = AsyncMock()
        mock_meshcore.commands.send_msg = AsyncMock()
        mock_meshcore.commands.send_chan_msg = AsyncMock()
        session._meshcore = mock_meshcore

        return session, mock_meshcore

    # -- send_chan_msg dispatch ------------------------------------------------

    async def test_channel_index_routes_to_send_chan_msg(self) -> None:
        """channel_index=3 calls send_chan_msg(3, text), NOT send_msg."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = {}

        await session.send_text("ignored_contact", "hello chan", channel_index=3)

        mock_mc.commands.send_chan_msg.assert_awaited_once_with(3, "hello chan")
        mock_mc.commands.send_msg.assert_not_awaited()

    async def test_no_channel_index_routes_to_send_msg(self) -> None:
        """channel_index=None calls send_msg(contact_id, text), NOT send_chan_msg."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\xaa\xbb\xcc\xdd"}

        await session.send_text("contact_abc", "hello dm")

        mock_mc.commands.send_msg.assert_awaited_once_with("contact_abc", "hello dm")
        mock_mc.commands.send_chan_msg.assert_not_awaited()

    async def test_channel_index_zero_routes_to_send_chan_msg(self) -> None:
        """channel_index=0 (first channel) still dispatches to send_chan_msg."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = {}

        await session.send_text("ignored", "msg", channel_index=0)

        mock_mc.commands.send_chan_msg.assert_awaited_once_with(0, "msg")
        mock_mc.commands.send_msg.assert_not_awaited()

    # -- ACK extraction for DMs ------------------------------------------------

    async def test_dm_4byte_ack_returned_as_hex(self) -> None:
        """DM with 4-byte expected_ack → hex native_id."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\xde\xad\xbe\xef",
        }

        result = await session.send_text("contact1", "test")
        assert result == "deadbeef"

    async def test_dm_ack_from_payload_dict(self) -> None:
        """Object result with .payload dict containing 4-byte expected_ack."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={"expected_ack": b"\x11\x22\x33\x44"},
        )

        result = await session.send_text("contact1", "test")
        assert result == "11223344"

    async def test_dm_ack_from_attributes_dict(self) -> None:
        """Object result with .attributes dict containing 4-byte expected_ack."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={},
            attributes={"expected_ack": b"\x00\xff\x00\xff"},
        )

        result = await session.send_text("contact1", "test")
        assert result == "00ff00ff"

    # -- Channel send returns None (no ACK protocol) --------------------------

    async def test_channel_send_dict_returns_none(self) -> None:
        """Channel send returning empty dict → None (no ACK protocol)."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = {}

        result = await session.send_text("ignored", "test", channel_index=1)
        assert result is None

    async def test_channel_send_object_returns_none(self) -> None:
        """Channel send returning MockEvent with empty payload → None."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = MockEvent(
            event_type=MockEventType.OK,
            payload={},
        )

        result = await session.send_text("ignored", "test", channel_index=2)
        assert result is None

    # -- 8-byte expected_ack triggers warning and returns None -----------------

    async def test_8byte_ack_triggers_warning_and_returns_none(self) -> None:
        """8-byte expected_ack triggers logger warning, returns None.

        The _extract_expected_ack function explicitly warns on non-4-byte
        values (possible SDK API change) and returns None.
        """
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04\x05\x06\x07\x08",
        }

        result = await session.send_text("contact1", "test")
        assert result is None


# ===================================================================
# Message delay (pacing)
# ===================================================================


class TestMessageDelayPacing:
    """Verify message_delay_seconds is respected in _send_real."""

    def _make_session_with_mock(
        self, delay: float = 0.0
    ) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with mock _meshcore and given delay."""
        config = _make_config(
            connection_type="tcp", host="localhost", message_delay_seconds=delay
        )
        session = MeshCoreSession(config, "delay-test")
        session._diag.connected = True

        mock_meshcore = AsyncMock()
        mock_meshcore.commands = AsyncMock()
        mock_meshcore.commands.send_msg = AsyncMock()
        mock_meshcore.commands.send_chan_msg = AsyncMock()
        session._meshcore = mock_meshcore

        return session, mock_meshcore

    async def test_delay_applied_when_positive(self) -> None:
        """When message_delay_seconds > 0, asyncio.sleep is called with that value."""
        from unittest.mock import patch

        session, mock_mc = self._make_session_with_mock(delay=1.5)
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await session.send_text("contact1", "test")
            # Find the pacing call (1.5), ignoring retry backoff calls.
            pacing_calls = [c for c in mock_sleep.call_args_list if c.args[0] == 1.5]
            assert len(pacing_calls) == 1

    async def test_no_delay_when_zero(self) -> None:
        """When message_delay_seconds == 0, no pacing sleep is added."""
        from unittest.mock import patch

        session, mock_mc = self._make_session_with_mock(delay=0.0)
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await session.send_text("contact1", "test")
            for call in mock_sleep.call_args_list:
                # Pacing sleep(0.0) should never happen — only backoff values.
                assert call.args[0] != 0.0

    async def test_sleep_duration_matches_config(self) -> None:
        """The exact configured delay value is passed to asyncio.sleep."""
        from unittest.mock import patch

        session, mock_mc = self._make_session_with_mock(delay=2.5)
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await session.send_text("contact1", "test")
            mock_sleep.assert_any_call(2.5)

    async def test_concurrent_sends_are_serialized(self) -> None:
        """Concurrent send_text calls are serialized by the send lock."""
        session, mock_mc = self._make_session_with_mock(delay=0.05)
        mock_mc.commands.send_msg.return_value = {"expected_ack": b"\x01\x02\x03\x04"}

        import asyncio

        results = await asyncio.gather(
            session.send_text("contact1", "msg1"),
            session.send_text("contact1", "msg2"),
        )
        # Both should succeed
        assert all(r is not None for r in results)
        # Sends should have been called twice (serialized, not concurrent)
        assert mock_mc.commands.send_msg.await_count == 2


# ===================================================================
# Auto message fetching
# ===================================================================


class TestAutoMessageFetching:
    """Verify start_auto_message_fetching is called on connect and stopped
    on disconnect."""

    def _setup_real_session(self) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session and install mock meshcore module."""
        from unittest.mock import patch

        from tests.helpers.meshcore_session import (
            build_mock_meshcore_module,
            install_mock_module,
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "auto-fetch-test")
        mock_mc, instance = build_mock_meshcore_module()
        install_mock_module(mock_mc)
        # Patch HAS_MESHCORE so _connect_real proceeds.
        self._has_mc_patcher = patch(
            "medre.adapters.meshcore.session.HAS_MESHCORE", True
        )
        self._has_mc_patcher.start()
        return session, instance

    def _teardown_mock_module(self) -> None:
        from tests.helpers.meshcore_session import remove_mock_module

        self._has_mc_patcher.stop()
        remove_mock_module()

    async def test_auto_message_fetching_called_on_connect(self) -> None:
        """start_auto_message_fetching is called during _connect_real."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            instance.start_auto_message_fetching.assert_awaited_once()
            assert session.connected is True
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_auto_message_fetching_graceful_on_failure(self) -> None:
        """If start_auto_message_fetching raises, connection still succeeds."""
        session, instance = self._setup_real_session()
        instance.start_auto_message_fetching = AsyncMock(
            side_effect=RuntimeError("fetch failed")
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            # Connection should still succeed despite fetch failure.
            assert session.connected is True
            instance.start_auto_message_fetching.assert_awaited_once()
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_auto_message_fetching_stopped_on_disconnect(self) -> None:
        """stop_auto_message_fetching is called during stop()."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            await session.stop()
            instance.stop_auto_message_fetching.assert_awaited_once()
        finally:
            self._teardown_mock_module()

    async def test_auto_message_fetching_not_required(self) -> None:
        """Connection works even if SDK lacks start_auto_message_fetching."""
        session, instance = self._setup_real_session()
        # Remove the method to simulate older SDK.
        del instance.start_auto_message_fetching

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            assert session.connected is True
        finally:
            await session.stop()
            self._teardown_mock_module()


# ===================================================================
# Self-info capture from send_appstart
# ===================================================================


class TestSelfInfoCapture:
    """Verify device self_info is captured from send_appstart result."""

    def _setup_real_session(
        self,
        appstart_payload: dict | None = None,
    ) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with mock meshcore returning given appstart payload."""
        from unittest.mock import patch

        from tests.helpers.meshcore_session import (
            MockEvent,
            MockEventType,
            build_mock_meshcore_module,
            install_mock_module,
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "selfinfo-test")
        mock_mc, instance = build_mock_meshcore_module()

        payload = appstart_payload if appstart_payload is not None else {}
        instance.commands.send_appstart = AsyncMock(
            return_value=MockEvent(event_type=MockEventType.OK, payload=payload)
        )

        install_mock_module(mock_mc)
        # Patch HAS_MESHCORE so _connect_real proceeds.
        self._has_mc_patcher = patch(
            "medre.adapters.meshcore.session.HAS_MESHCORE", True
        )
        self._has_mc_patcher.start()
        return session, instance

    def _teardown_mock_module(self) -> None:
        from tests.helpers.meshcore_session import remove_mock_module

        self._has_mc_patcher.stop()
        remove_mock_module()

    async def test_device_name_captured(self) -> None:
        """Device name extracted from appstart payload."""
        session, _ = self._setup_real_session(
            appstart_payload={"name": "MyNode42", "public_key": "aabbccdd"}
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["device_name"] == "MyNode42"
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_public_key_prefix_captured(self) -> None:
        """Public key prefix (first 12 hex chars) extracted from appstart."""
        session, _ = self._setup_real_session(
            appstart_payload={
                "name": "node",
                "public_key": "aabbccddeeff00112233445566778899",
            }
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["public_key_prefix"] == "aabbccddeeff"
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_radio_freq_captured(self) -> None:
        """Radio frequency extracted from appstart payload."""
        session, _ = self._setup_real_session(
            appstart_payload={"name": "node", "freq": 868.0}
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["radio_freq"] == 868.0
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_empty_payload_leaves_defaults(self) -> None:
        """Empty appstart payload leaves all self_info fields as None."""
        session, _ = self._setup_real_session(appstart_payload={})

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["device_name"] is None
            assert diag["public_key_prefix"] is None
            assert diag["radio_freq"] is None
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_public_key_bytes_prefix(self) -> None:
        """Public key as bytes is converted to hex prefix."""
        session, _ = self._setup_real_session(
            appstart_payload={
                "name": "node",
                "public_key": b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11",
            }
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["public_key_prefix"] == "aabbccddeeff"
        finally:
            await session.stop()
            self._teardown_mock_module()

    async def test_public_key_normalized_to_lowercase(self) -> None:
        """Uppercase hex pubkey is normalized to lowercase prefix."""
        config = _make_config()
        session = MeshCoreSession(config, "test-1")
        session._capture_self_info({"public_key": "AABBCCDDEEFF0011223344"})
        assert session.diagnostics()["public_key_prefix"] == "aabbccddeeff"

    async def test_short_public_key_no_prefix(self) -> None:
        """Public key shorter than 12 hex chars leaves prefix as None."""
        session, _ = self._setup_real_session(
            appstart_payload={"name": "node", "public_key": "abc"}
        )

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            diag = session.diagnostics()
            assert diag["public_key_prefix"] is None
        finally:
            await session.stop()
            self._teardown_mock_module()


# ===================================================================
# SDK suggested_timeout caching for DM retry delays
# ===================================================================


class TestSdkRetryDelayCaching:
    """Verify suggested_timeout is cached as instance attribute across
    send_text() calls and cleared on stop().

    The cached value (_sdk_retry_delay) persists so that a successful DM
    that captures a timeout can inform the retry delay of a subsequent
    failing DM within the same session lifecycle.
    """

    def _make_session_with_mock(self) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with connected=True and a mock _meshcore."""
        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "retry-delay-test")
        session._diag.connected = True

        mock_meshcore = AsyncMock()
        mock_meshcore.commands = AsyncMock()
        mock_meshcore.commands.send_msg = AsyncMock()
        mock_meshcore.commands.send_chan_msg = AsyncMock()
        session._meshcore = mock_meshcore

        return session, mock_meshcore

    async def test_successful_dm_captures_timeout(self) -> None:
        """A successful DM with suggested_timeout caches the value."""
        session, mock_mc = self._make_session_with_mock()
        # 5000ms → 5.0s, clamped within [0.5, 30.0]
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        await session.send_text("contact1", "test")
        assert session._sdk_retry_delay == 5.0
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1

    async def test_failing_dm_retries_with_cached_timeout(self) -> None:
        """A failing DM retries using previously cached suggested_timeout."""
        session, mock_mc = self._make_session_with_mock()

        from unittest.mock import patch

        # First call: succeed with timeout 3000ms → 3.0s
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 3000,
        }
        await session.send_text("contact1", "first")
        assert session._sdk_retry_delay == 3.0

        # Second call: fail on attempt 1, succeed on attempt 2
        call_count = 0

        async def _fail_then_succeed(cid: str, text: str) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return {"expected_ack": b"\x05\x06\x07\x08"}

        mock_mc.commands.send_msg.side_effect = _fail_then_succeed

        original_sleep = AsyncMock()

        with patch("asyncio.sleep", original_sleep):
            await session.send_text("contact1", "second")
            # Verify retry sleep used the cached timeout (3.0s)
            sleep_calls = [c.args[0] for c in original_sleep.call_args_list]
            assert 3.0 in sleep_calls

    async def test_sdk_retry_delay_cleared_on_stop(self) -> None:
        """stop() clears the cached _sdk_retry_delay."""
        session, mock_mc = self._make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        await session.send_text("contact1", "test")
        assert session._sdk_retry_delay is not None

        # Mark started so stop() actually runs cleanup
        session._started = True
        await session.stop()
        assert session._sdk_retry_delay is None


# ===================================================================
# stop() resets self-info diagnostics
# ===================================================================


class TestStopResetsSelfInfoDiagnostics:
    """Verify that stop() clears device_name, public_key_prefix, and
    radio_freq so stale values don't persist across lifecycle boundaries."""

    def _setup_real_session(
        self,
        appstart_payload: dict | None = None,
    ) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with mock meshcore returning given appstart payload."""
        from unittest.mock import patch

        from tests.helpers.meshcore_session import (
            MockEvent,
            MockEventType,
            build_mock_meshcore_module,
            install_mock_module,
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "stop-selfinfo-test")
        mock_mc, instance = build_mock_meshcore_module()

        payload = appstart_payload if appstart_payload is not None else {}
        instance.commands.send_appstart = AsyncMock(
            return_value=MockEvent(event_type=MockEventType.OK, payload=payload)
        )

        install_mock_module(mock_mc)
        self._has_mc_patcher = patch(
            "medre.adapters.meshcore.session.HAS_MESHCORE", True
        )
        self._has_mc_patcher.start()
        return session, instance

    def _teardown_mock_module(self) -> None:
        from tests.helpers.meshcore_session import remove_mock_module

        self._has_mc_patcher.stop()
        remove_mock_module()

    async def test_stop_clears_device_name(self) -> None:
        """stop() resets device_name to None."""
        session, _ = self._setup_real_session(
            appstart_payload={"name": "TestNode", "public_key": "aabbccddeeff0011"}
        )

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        assert session.diagnostics()["device_name"] == "TestNode"
        await session.stop()
        assert session.diagnostics()["device_name"] is None
        self._teardown_mock_module()

    async def test_stop_clears_public_key_prefix(self) -> None:
        """stop() resets public_key_prefix to None."""
        session, _ = self._setup_real_session(
            appstart_payload={"public_key": "aabbccddeeff001122334455"}
        )

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        assert session.diagnostics()["public_key_prefix"] == "aabbccddeeff"
        await session.stop()
        assert session.diagnostics()["public_key_prefix"] is None
        self._teardown_mock_module()

    async def test_stop_clears_radio_freq(self) -> None:
        """stop() resets radio_freq to None."""
        session, _ = self._setup_real_session(appstart_payload={"freq": 915.0})

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)
        assert session.diagnostics()["radio_freq"] == 915.0
        await session.stop()
        assert session.diagnostics()["radio_freq"] is None
        self._teardown_mock_module()
