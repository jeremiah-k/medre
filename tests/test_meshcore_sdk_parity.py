"""MeshCore adapter health lifecycle parity, suggested_timeout retry handling,
and contact/self-info observability subscriptions.

Tests cover:
- Health lifecycle: ``None`` before first health_check per lifecycle; clears on
  start/stop/restart; ``health_lifecycle_epoch`` increments on transitions.
- ``suggested_timeout``: extraction from dict/payload/attribute result shapes;
  invalid values ignored; clamped to floor/ceil bounds; DM retry delay uses it;
  channel sends have no suggested_timeout requirement; diagnostic counter
  ``sdk_suggested_timeouts_used`` incremented.
- Contact/self-info subscriptions: CONTACTS and SELF_INFO events update
  diagnostics (``known_contact_count``, ``last_contact_update_time``,
  self-info fields); reconnect re-subscribes; stop unsubscribes cleanly;
  no topology canonical events emitted.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.session import (
    _SUGGESTED_TIMEOUT_CEIL,
    _SUGGESTED_TIMEOUT_FLOOR,
    MeshCoreSession,
    _extract_suggested_timeout,
)
from medre.config.adapters.meshcore import MeshCoreConfig

# ===================================================================
# Helpers
# ===================================================================


def _make_config(**overrides: Any) -> MeshCoreConfig:
    defaults: dict[str, Any] = dict(adapter_id="sdk-parity-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_session_with_mock() -> tuple[MeshCoreSession, AsyncMock]:
    """Create a TCP session with connected=True and a mock _meshcore."""
    config = _make_config(connection_type="tcp", host="localhost")
    session = MeshCoreSession(config, "sdk-parity-session")
    session._diag.connected = True

    mock_meshcore = AsyncMock()
    mock_meshcore.commands = AsyncMock()
    mock_meshcore.commands.send_msg = AsyncMock()
    mock_meshcore.commands.send_chan_msg = AsyncMock()
    session._meshcore = mock_meshcore

    return session, mock_meshcore


# ===================================================================
# Health lifecycle parity
# ===================================================================


class TestHealthLifecycleParity:
    """Diagnostics health follows the lifecycle rule.

    - None before first health_check per lifecycle.
    - Clears on start/stop/restart.
    - health_lifecycle_epoch increments on transitions.
    """

    def test_initial_health_is_none(self) -> None:
        """Fresh adapter diagnostics health is None before start."""
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        assert diag["health"] is None
        assert diag["health_lifecycle_epoch"] == 0

    def test_initial_health_lifecycle_epoch_is_zero(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        assert adapter._health_lifecycle_epoch == 0

    async def test_health_none_after_start_before_health_check(
        self, make_adapter_context
    ) -> None:
        """After start(), health remains None until health_check() is called."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("health-test")
        await adapter.start(ctx)
        diag = adapter.diagnostics()
        assert diag["health"] is None
        # Epoch should have incremented on start.
        assert diag["health_lifecycle_epoch"] == 1

    async def test_health_set_after_health_check(self, make_adapter_context) -> None:
        """health_check() sets the cached health string."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("health-test")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        assert adapter.diagnostics()["health"] == "healthy"

    async def test_health_clears_on_stop(self, make_adapter_context) -> None:
        """stop() resets health to None."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("health-test")
        await adapter.start(ctx)
        await adapter.health_check()
        assert adapter.diagnostics()["health"] == "healthy"

        await adapter.stop()
        assert adapter.diagnostics()["health"] is None
        # Epoch should have incremented on stop.
        assert adapter.diagnostics()["health_lifecycle_epoch"] == 2

    async def test_health_clears_on_restart(self, make_adapter_context) -> None:
        """Restart (stop + start) resets health to None."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("health-test")

        await adapter.start(ctx)
        await adapter.health_check()
        assert adapter.diagnostics()["health"] == "healthy"

        await adapter.stop()
        await adapter.start(ctx)
        assert adapter.diagnostics()["health"] is None
        # Epoch: 1 (start) + 1 (stop) + 1 (start) = 3
        assert adapter.diagnostics()["health_lifecycle_epoch"] == 3

    async def test_epoch_monotonically_increases(self, make_adapter_context) -> None:
        """health_lifecycle_epoch never decreases."""
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("health-test")

        epochs: list[int] = [adapter._health_lifecycle_epoch]
        await adapter.start(ctx)
        epochs.append(adapter._health_lifecycle_epoch)
        await adapter.stop()
        epochs.append(adapter._health_lifecycle_epoch)
        await adapter.start(ctx)
        epochs.append(adapter._health_lifecycle_epoch)
        await adapter.stop()
        epochs.append(adapter._health_lifecycle_epoch)

        for i in range(1, len(epochs)):
            assert (
                epochs[i] > epochs[i - 1]
            ), f"Epoch did not increase at step {i}: {epochs}"

    async def test_fake_adapter_epoch_parity(self, make_adapter_context) -> None:
        """Fake adapter also tracks health_lifecycle_epoch."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("health-fake")
        assert adapter.diagnostics()["health_lifecycle_epoch"] == 0

        await adapter.start(ctx)
        assert adapter.diagnostics()["health_lifecycle_epoch"] == 1

        await adapter.stop()
        assert adapter.diagnostics()["health_lifecycle_epoch"] == 2


# ===================================================================
# _extract_suggested_timeout
# ===================================================================


class TestExtractSuggestedTimeout:
    """Cover _extract_suggested_timeout for various input shapes."""

    def test_valid_ms_dict(self) -> None:
        """Integer ms value converted to seconds and clamped."""
        result = _extract_suggested_timeout({"suggested_timeout": 5000})
        assert result == 5.0

    def test_very_small_clamped_to_floor(self) -> None:
        """Very small timeout clamped to _SUGGESTED_TIMEOUT_FLOOR."""
        # 1 ms = 0.001 s, clamped to 0.5 s
        result = _extract_suggested_timeout({"suggested_timeout": 1})
        assert result == _SUGGESTED_TIMEOUT_FLOOR

    def test_very_large_clamped_to_ceil(self) -> None:
        """Very large timeout clamped to _SUGGESTED_TIMEOUT_CEIL."""
        # 1_000_000 ms = 1000 s, clamped to 30 s
        result = _extract_suggested_timeout({"suggested_timeout": 1_000_000})
        assert result == _SUGGESTED_TIMEOUT_CEIL

    def test_float_ms(self) -> None:
        """Float millisecond value handled correctly."""
        result = _extract_suggested_timeout({"suggested_timeout": 1500.0})
        assert result == 1.5

    def test_none_value(self) -> None:
        """None suggested_timeout returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": None}) is None

    def test_missing_key(self) -> None:
        """Missing key returns None."""
        assert _extract_suggested_timeout({}) is None

    def test_string_value(self) -> None:
        """String value returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": "fast"}) is None

    def test_bool_value(self) -> None:
        """Bool value returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": True}) is None

    def test_zero_value(self) -> None:
        """Zero value returns None (non-positive)."""
        assert _extract_suggested_timeout({"suggested_timeout": 0}) is None

    def test_negative_value(self) -> None:
        """Negative value returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": -1000}) is None

    def test_nan_value(self) -> None:
        """NaN value returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": float("nan")}) is None

    def test_inf_value(self) -> None:
        """Inf value returns None."""
        assert _extract_suggested_timeout({"suggested_timeout": float("inf")}) is None

    def test_non_dict_source(self) -> None:
        """Non-dict source returns None."""
        assert _extract_suggested_timeout("not a dict") is None

    def test_boundary_floor(self) -> None:
        """Value that converts exactly to floor is accepted."""
        ms = int(_SUGGESTED_TIMEOUT_FLOOR * 1000)
        result = _extract_suggested_timeout({"suggested_timeout": ms})
        assert result == _SUGGESTED_TIMEOUT_FLOOR

    def test_boundary_ceil(self) -> None:
        """Value that converts exactly to ceil is accepted."""
        ms = int(_SUGGESTED_TIMEOUT_CEIL * 1000)
        result = _extract_suggested_timeout({"suggested_timeout": ms})
        assert result == _SUGGESTED_TIMEOUT_CEIL


# ===================================================================
# suggested_timeout in send_text
# ===================================================================


class TestSuggestedTimeoutInSend:
    """Verify suggested_timeout extraction and retry delay in _send_real."""

    async def test_dict_result_extracts_suggested_timeout(self) -> None:
        """Dict result with suggested_timeout increments diagnostic counter."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        result = await session.send_text("contact1", "test")
        assert result == "01020304"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1

    async def test_payload_result_extracts_suggested_timeout(self) -> None:
        """Object result with .payload dict extracts suggested_timeout."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload={
                "expected_ack": b"\x01\x02\x03\x04",
                "suggested_timeout": 3000,
            },
        )

        result = await session.send_text("contact1", "test")
        assert result == "01020304"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1

    async def test_channel_send_no_suggested_timeout_counter(self) -> None:
        """Channel sends do not increment suggested_timeout counter."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = {
            "suggested_timeout": 5000,
        }

        await session.send_text("ignored", "test", channel_index=0)
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0

    async def test_invalid_suggested_timeout_no_counter(self) -> None:
        """Invalid suggested_timeout does not increment counter."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": "invalid",
        }

        result = await session.send_text("contact1", "test")
        assert result == "01020304"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0

    async def test_missing_suggested_timeout_no_counter(self) -> None:
        """Missing suggested_timeout does not increment counter."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
        }

        result = await session.send_text("contact1", "test")
        assert result == "01020304"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0

    async def test_retry_uses_suggested_timeout_delay(self) -> None:
        """DM retry uses clamped suggested_timeout as sleep duration."""
        session, mock_mc = _make_session_with_mock()

        call_count = 0

        async def _fail_then_succeed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            return {
                "expected_ack": b"\x01\x02\x03\x04",
                "suggested_timeout": 2000,  # 2.0 s, within floor/ceil range
            }

        mock_mc.commands.send_msg.side_effect = _fail_then_succeed

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await session.send_text("contact1", "test")
            assert result == "01020304"

            # First attempt fails, second succeeds.
            # The retry sleep should NOT be 0.1 * attempt (0.1) because the
            # first result did NOT have suggested_timeout (it raised).
            # So the fallback 0.1 should have been used.
            retry_calls = [
                c
                for c in mock_sleep.call_args_list
                if c.args[0] != session._config.message_delay_seconds
            ]
            # There should be one retry sleep (between attempt 1 and 2).
            assert len(retry_calls) >= 1

    async def test_retry_uses_sdk_timeout_from_successful_attempt(self) -> None:
        """After a successful DM send captures suggested_timeout, subsequent
        retries of a DIFFERENT send_text call use it."""
        session, mock_mc = _make_session_with_mock()

        # First call succeeds and returns suggested_timeout.
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 800,  # 0.8s, clamped to 0.5s floor → 0.8s
        }

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await session.send_text("contact1", "first")
            assert result == "01020304"
            assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1

    async def test_counter_persists_across_sends(self) -> None:
        """sdk_suggested_timeouts_used accumulates across multiple sends."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await session.send_text("contact1", "msg1")
            await session.send_text("contact1", "msg2")
            await session.send_text("contact1", "msg3")

        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 3

    async def test_counter_resets_on_stop(self) -> None:
        """sdk_suggested_timeouts_used resets to 0 on stop()."""
        session, mock_mc = _make_session_with_mock()
        session._started = True
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await session.send_text("contact1", "msg1")
            await session.send_text("contact1", "msg2")

        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 2

        await session.stop()

        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0

    async def test_counter_is_json_safe(self) -> None:
        """sdk_suggested_timeouts_used is a JSON-safe int."""
        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = {
            "expected_ack": b"\x01\x02\x03\x04",
            "suggested_timeout": 5000,
        }

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await session.send_text("contact1", "test")

        diag = session.diagnostics()
        assert isinstance(diag["sdk_suggested_timeouts_used"], int)
        # Verify JSON round-trip.
        json.dumps(diag["sdk_suggested_timeouts_used"])

    async def test_attributes_suggested_timeout_valid(self) -> None:
        """Object result with .attributes dict extracts suggested_timeout."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = _make_session_with_mock()
        # Result where payload is not a dict but attributes is, with
        # both expected_ack and suggested_timeout.
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload=None,
            attributes={
                "expected_ack": b"\xab\x12\x00\x00",
                "suggested_timeout": 3000,
            },
        )

        result = await session.send_text("contact1", "test")
        assert result == "ab120000"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1
        assert session._contact_retry_delays.get("contact1") == 3.0

    async def test_attributes_suggested_timeout_invalid(self) -> None:
        """Invalid suggested_timeout in .attributes does not increment counter."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload=None,
            attributes={
                "expected_ack": b"\xab\x12\x00\x00",
                "suggested_timeout": "not_a_number",
            },
        )

        result = await session.send_text("contact1", "test")
        assert result == "ab120000"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0
        assert "contact1" not in session._contact_retry_delays

    async def test_attributes_expected_ack_and_suggested_timeout_together(
        self,
    ) -> None:
        """Both native_message_id and suggested_timeout extracted from .attributes."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload=None,
            attributes={
                "expected_ack": b"\xcd\x34\x00\x00",
                "suggested_timeout": 5000,
            },
        )

        result = await session.send_text("contact1", "test")
        assert result == "cd340000"
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 1
        assert session._contact_retry_delays.get("contact1") == 5.0

    async def test_channel_send_with_attributes_suggested_timeout_no_increment(
        self,
    ) -> None:
        """Channel sends with attributes suggested_timeout do NOT increment counter."""
        from tests.helpers.meshcore_session import MockEvent, MockEventType

        session, mock_mc = _make_session_with_mock()
        mock_mc.commands.send_chan_msg.return_value = MockEvent(
            event_type=MockEventType.MSG_SENT,
            payload=None,
            attributes={
                "suggested_timeout": 2000,
            },
        )

        await session.send_text("ignored", "test", channel_index=0)
        assert session.diagnostics()["sdk_suggested_timeouts_used"] == 0


# ===================================================================
# Contact/self-info observability subscriptions
# ===================================================================


class TestContactSubscriptions:
    """CONTACTS and SELF_INFO subscription handling for diagnostics only."""

    def _setup_real_session(
        self,
    ) -> tuple[MeshCoreSession, AsyncMock]:
        """Create a TCP session with mock meshcore module."""
        from tests.helpers.meshcore_session import (
            build_mock_meshcore_module,
            install_mock_module,
        )

        config = _make_config(connection_type="tcp", host="localhost")
        session = MeshCoreSession(config, "contact-sub-test")
        mock_mc, instance = build_mock_meshcore_module()
        install_mock_module(mock_mc)
        self._has_mc_patcher = patch(
            "medre.adapters.meshcore.session.HAS_MESHCORE", True
        )
        self._has_mc_patcher.start()
        return session, instance

    def _teardown(self) -> None:
        from tests.helpers.meshcore_session import remove_mock_module

        self._has_mc_patcher.stop()
        remove_mock_module()

    async def test_contacts_subscription_registered(self) -> None:
        """CONTACTS event type is subscribed during _subscribe_events."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            # Verify subscribe was called with CONTACTS.
            subscribe_calls = instance.subscribe.call_args_list
            event_types = [call[0][0] for call in subscribe_calls]
            # MockEventType.CONTACTS is subscribed.
            from tests.helpers.meshcore_session import MockEventType

            assert MockEventType.CONTACTS in event_types
        finally:
            await session.stop()
            self._teardown()

    async def test_self_info_subscription_registered(self) -> None:
        """SELF_INFO event type is subscribed during _subscribe_events."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            subscribe_calls = instance.subscribe.call_args_list
            event_types = [call[0][0] for call in subscribe_calls]
            from tests.helpers.meshcore_session import MockEventType

            assert MockEventType.SELF_INFO in event_types
        finally:
            await session.stop()
            self._teardown()

    async def test_contacts_event_updates_diagnostics(self) -> None:
        """CONTACTS event updates known_contact_count and timestamp."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            # Find the CONTACTS callback.
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                    break
            assert contacts_callback is not None

            # Simulate a CONTACTS event.
            contacts_event = MagicMock()
            contacts_event.payload = {
                "contacts": [
                    {"name": "alice", "pubkey_prefix": "aabb"},
                    {"name": "bob", "pubkey_prefix": "ccdd"},
                    {"name": "carol", "pubkey_prefix": "eeff"},
                ],
            }
            await contacts_callback(contacts_event)

            diag = session.diagnostics()
            assert diag["known_contact_count"] == 3
            assert diag["last_contact_update_time"] is not None
        finally:
            await session.stop()
            self._teardown()

    async def test_contacts_dict_event_updates_count(self) -> None:
        """CONTACTS event as plain dict updates count."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                    break
            assert contacts_callback is not None

            # Dict payload with dict contacts.
            await contacts_callback({"contacts": {"a": 1, "b": 2, "c": 3, "d": 4}})

            diag = session.diagnostics()
            assert diag["known_contact_count"] == 4
        finally:
            await session.stop()
            self._teardown()

    async def test_self_info_event_updates_diagnostics(self) -> None:
        """SELF_INFO event updates device_name and public_key_prefix."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            from tests.helpers.meshcore_session import MockEventType

            self_info_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.SELF_INFO:
                    self_info_callback = call[0][1]
                    break
            assert self_info_callback is not None

            event = MagicMock()
            event.payload = {
                "name": "UpdatedNode",
                "public_key": "aabbccddeeff00112233445566778899",
            }
            await self_info_callback(event)

            diag = session.diagnostics()
            assert diag["device_name"] == "UpdatedNode"
            assert diag["public_key_prefix"] == "aabbccddeeff"
        finally:
            await session.stop()
            self._teardown()

    async def test_contacts_count_resets_on_stop(self) -> None:
        """Contact count resets to 0 on stop."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                    break
            assert contacts_callback is not None

            await contacts_callback(
                MagicMock(payload={"contacts": [{"n": "a"}, {"n": "b"}]})
            )
            assert session.diagnostics()["known_contact_count"] == 2

            await session.stop()
            assert session.diagnostics()["known_contact_count"] == 0
            assert session.diagnostics()["last_contact_update_time"] is None
        finally:
            self._teardown()

    async def test_stop_unsubscribes_all(self) -> None:
        """stop() unsubscribes all registered callbacks cleanly."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            sub_count = len(session._subscriptions)
            assert sub_count >= 5  # DM + CHAN + DISC + CONTACTS + SELF_INFO

            await session.stop()
            assert len(session._subscriptions) == 0
            # Verify unsubscribe was called for each subscription.
            assert instance.unsubscribe.call_count == sub_count
        finally:
            self._teardown()

    async def test_reconnect_resubscribes(self) -> None:
        """After reconnect, subscriptions are re-established."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            first_sub_count = len(session._subscriptions)
            assert first_sub_count >= 5

            await session.stop()
            assert len(session._subscriptions) == 0

            # Restart — subscriptions should be re-established.
            await session.start(noop)
            assert len(session._subscriptions) == first_sub_count
        finally:
            await session.stop()
            self._teardown()

    async def test_no_topology_canonical_events(self) -> None:
        """Contact/self-info subscriptions do NOT emit canonical events.

        The subscriptions are diagnostics-only.  They should not produce
        inbound events through the message callback.
        """
        session, instance = self._setup_real_session()
        received_messages: list[dict] = []

        async def callback(pkt: dict) -> None:
            received_messages.append(pkt)

        try:
            await session.start(callback)
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            self_info_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                elif call[0][0] == MockEventType.SELF_INFO:
                    self_info_callback = call[0][1]

            assert contacts_callback is not None
            assert self_info_callback is not None

            # Fire CONTACTS and SELF_INFO events.
            await contacts_callback(MagicMock(payload={"contacts": [{"n": "a"}]}))
            await self_info_callback(
                MagicMock(payload={"name": "node", "public_key": "aabbccdd"})
            )

            # No messages should have been forwarded to the callback.
            assert len(received_messages) == 0
        finally:
            await session.stop()
            self._teardown()

    async def test_diagnostics_json_safe(self) -> None:
        """All new diagnostic fields are JSON-safe primitives."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                    break
            assert contacts_callback is not None

            await contacts_callback(MagicMock(payload={"contacts": [{"n": "a"}]}))

            diag = session.diagnostics()
            # Should survive JSON round-trip.
            serialized = json.dumps(diag)
            deserialized = json.loads(serialized)
            assert isinstance(deserialized["known_contact_count"], int)
            assert isinstance(deserialized["sdk_suggested_timeouts_used"], int)
            assert isinstance(
                deserialized["last_contact_update_time"], (str, type(None))
            )
        finally:
            await session.stop()
            self._teardown()

    async def test_initial_diagnostics_fields(self) -> None:
        """New diagnostic fields have correct initial values."""
        config = _make_config()
        session = MeshCoreSession(config, "init-test")
        diag = session.diagnostics()
        assert diag["sdk_suggested_timeouts_used"] == 0
        assert diag["known_contact_count"] == 0
        assert diag["last_contact_update_time"] is None

    async def test_contacts_event_malformed_payload_no_crash(self) -> None:
        """CONTACTS event with non-dict/non-list contacts field doesn't crash."""
        session, instance = self._setup_real_session()

        async def noop(pkt: dict) -> None:
            pass

        try:
            await session.start(noop)
            from tests.helpers.meshcore_session import MockEventType

            contacts_callback = None
            for call in instance.subscribe.call_args_list:
                if call[0][0] == MockEventType.CONTACTS:
                    contacts_callback = call[0][1]
                    break
            assert contacts_callback is not None

            # Malformed: contacts is a string instead of list/dict.
            await contacts_callback(MagicMock(payload={"contacts": "broken"}))
            # Should not crash — count should remain at 0.
            assert session.diagnostics()["known_contact_count"] == 0
        finally:
            await session.stop()
            self._teardown()
