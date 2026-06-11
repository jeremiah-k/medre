"""Tests for MeshCore per-contact retry timeout cache clearing.

Verifies that ``_contact_retry_delays`` is cleared on lifecycle boundaries:
  - ``stop()``
  - ``_cleanup_failed_start()``
  - successful ``_reconnect_loop()``

Also verifies that per-contact cache isolation works correctly and channel
sends never consult the DM timeout cache.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from medre.adapters.meshcore.errors import MeshCoreConnectionError
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="timeout-cache-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_tcp_session() -> tuple[MeshCoreSession, AsyncMock]:
    """Create a TCP session with connected=True and a mock _meshcore."""
    config = _make_config(connection_type="tcp", host="localhost")
    session = MeshCoreSession(config, "retry-cache-session")
    session._diag.connected = True

    mock_meshcore = AsyncMock()
    mock_meshcore.commands = AsyncMock()
    mock_meshcore.commands.send_msg = AsyncMock()
    mock_meshcore.commands.send_chan_msg = AsyncMock()
    session._meshcore = mock_meshcore

    return session, mock_meshcore


# ===================================================================
# Test 1: stop() clears the cache
# ===================================================================


async def test_timeout_cache_clears_after_stop() -> None:
    """Start session, send DM that captures suggested_timeout, verify
    sdk_contact_timeout_count >= 1, stop, verify count == 0."""
    session, mock_mc = _make_tcp_session()
    session._started = True

    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }

    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("contact-a", "hello")

    assert session.diagnostics()["sdk_contact_timeout_count"] >= 1

    await session.stop()
    assert session.diagnostics()["sdk_contact_timeout_count"] == 0
    assert len(session._contact_retry_delays) == 0


# ===================================================================
# Test 2: _cleanup_failed_start() clears the cache
# ===================================================================


async def test_timeout_cache_clears_after_failed_start_cleanup() -> None:
    """Configure tcp mode but make connection fail; verify
    _contact_retry_delays is empty after the failed start."""
    config = _make_config(connection_type="tcp", host="localhost")
    session = MeshCoreSession(config, "fail-start-cache-test")

    # Inject stale data into the cache to prove cleanup clears it.
    session._contact_retry_delays["stale-contact"] = 3.0

    async def noop(pkt: dict) -> None:
        pass

    with (
        patch("medre.adapters.meshcore.session.HAS_MESHCORE", False),
        pytest.raises(MeshCoreConnectionError),
    ):
        await session.start(noop)

    # _cleanup_failed_start should have cleared the cache.
    assert len(session._contact_retry_delays) == 0
    assert session.diagnostics()["sdk_contact_timeout_count"] == 0


# ===================================================================
# Test 3: successful reconnect clears the cache
# ===================================================================


async def test_timeout_cache_clears_on_successful_reconnect() -> None:
    """Start session, send DM that captures timeout, verify count > 0.
    Then simulate reconnect by calling _reconnect_loop with _connect_real
    mocked to succeed.  After reconnect, verify cache is cleared."""
    import medre.adapters.meshcore.session as session_mod

    orig_base = session_mod._RECONNECT_BASE_DELAY
    orig_max_delay = session_mod._RECONNECT_MAX_DELAY
    orig_max_attempts = session_mod._RECONNECT_MAX_ATTEMPTS
    orig_jitter = session_mod._RECONNECT_JITTER_FRACTION

    session_mod._RECONNECT_BASE_DELAY = 0.001
    session_mod._RECONNECT_MAX_DELAY = 0.002
    session_mod._RECONNECT_MAX_ATTEMPTS = 5
    session_mod._RECONNECT_JITTER_FRACTION = 0.0

    try:
        config = _make_config(connection_type="fake")
        session = MeshCoreSession(config, "reconnect-cache-test")

        async def noop(pkt: dict) -> None:
            pass

        await session.start(noop)

        # Inject cached timeout entries to simulate pre-disconnect state.
        session._contact_retry_delays["contact-x"] = 2.5
        session._contact_retry_delays["contact-y"] = 4.0
        assert session.diagnostics()["sdk_contact_timeout_count"] == 2

        await session.stop()

        # Set up reconnect scenario: mock _connect_real to succeed.
        async def _succeed_connect():
            session._diag.connected = True

        session._connect_real = _succeed_connect
        session._stop_requested = False

        # Re-inject stale data to simulate cache surviving disconnect
        # (before the fix, these would persist across reconnect).
        session._contact_retry_delays["contact-x"] = 2.5
        session._contact_retry_delays["contact-y"] = 4.0

        with patch(
            "medre.adapters.meshcore.session.asyncio.sleep",
            new_callable=AsyncMock,
        ):
            await session._reconnect_loop()

        # After successful reconnect, cache should be cleared.
        assert len(session._contact_retry_delays) == 0
        assert session.diagnostics()["sdk_contact_timeout_count"] == 0
        assert session.connected is True
    finally:
        session_mod._RECONNECT_BASE_DELAY = orig_base
        session_mod._RECONNECT_MAX_DELAY = orig_max_delay
        session_mod._RECONNECT_MAX_ATTEMPTS = orig_max_attempts
        session_mod._RECONNECT_JITTER_FRACTION = orig_jitter


# ===================================================================
# Test 4: contact A's timeout does not affect contact B
# ===================================================================


async def test_timeout_for_contact_A_does_not_affect_contact_B() -> None:
    """Send DM to contact A (captures timeout), verify count == 1.
    Send DM to contact B (no timeout in result), verify B's retry uses
    linear fallback not A's cached value.  Diagnostics shows only A's
    hint is cached."""
    session, mock_mc = _make_tcp_session()

    # Send to contact A — captures suggested_timeout.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("contact-a", "msg to A")

    assert session.diagnostics()["sdk_contact_timeout_count"] == 1
    assert session._contact_retry_delays.get("contact-a") == 2.0

    # Send to contact B — no suggested_timeout in result.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x03\x04",
        # No suggested_timeout key.
    }

    call_count = 0

    async def _fail_once_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"expected_ack": b"\x05\x06"}

    mock_mc.commands.send_msg.side_effect = _fail_once_then_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("contact-b", "msg to B")
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # B should use linear fallback 0.1 * attempt = 0.1, NOT A's 2.0.
        assert retry_sleeps[0].args[0] == 0.1

    # Only contact A is cached, not B.
    assert session.diagnostics()["sdk_contact_timeout_count"] == 1
    assert "contact-b" not in session._contact_retry_delays


# ===================================================================
# Test 5: channel sends never consult DM timeout cache
# ===================================================================


async def test_channel_sends_never_consult_dm_timeout_cache() -> None:
    """Send a channel message.  Even if a DM timeout is cached for a
    contact, the channel send retry still uses linear fallback and
    the cache is not consulted."""
    session, mock_mc = _make_tcp_session()

    # Cache a DM timeout for contact-a.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("contact-a", "dm msg")

    assert session._contact_retry_delays.get("contact-a") == 2.0

    # Send a channel message that fails once then succeeds.
    call_count = 0

    async def _fail_once_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    mock_mc.commands.send_chan_msg.side_effect = _fail_once_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("ignored", "chan msg", channel_index=0)
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # Should use fallback 0.1 * attempt = 0.1, NOT cached 2.0.
        assert retry_sleeps[0].args[0] == 0.1

    # DM cache is untouched — channel send did not read or write it.
    assert session._contact_retry_delays.get("contact-a") == 2.0


# ===================================================================
# Test 6: normalized key — lowercase match
# ===================================================================


async def test_normalized_key_lowercase_match() -> None:
    """Send DM to 'AABBCC' that captures timeout, then verify retry to
    'aabbcc' uses the cached timeout (not linear fallback)."""
    session, mock_mc = _make_tcp_session()

    # First send: captures suggested_timeout for "AABBCC".
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("AABBCC", "hello")

    assert session._contact_retry_delays.get("aabbcc") == 2.0

    # Second send: fail once to trigger retry path, using lowercase "aabbcc".
    call_count = 0

    async def _fail_once_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"expected_ack": b"\x03\x04"}

    mock_mc.commands.send_msg.side_effect = _fail_once_then_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("aabbcc", "retry msg")
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # Should use cached 2.0, NOT linear fallback 0.1.
        assert retry_sleeps[0].args[0] == 2.0


# ===================================================================
# Test 7: normalized key — whitespace stripped
# ===================================================================


async def test_normalized_key_whitespace_stripped() -> None:
    """Send DM to '  AABBCC  ' that captures timeout, then verify retry
    to 'AABBCC' (no whitespace) uses cached timeout."""
    session, mock_mc = _make_tcp_session()

    # First send: captures suggested_timeout for "  AABBCC  ".
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 3000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("  AABBCC  ", "hello")

    # Normalized key should be "aabbcc" (stripped + lowercased).
    assert session._contact_retry_delays.get("aabbcc") == 3.0

    # Second send: fail once to trigger retry path with "AABBCC".
    call_count = 0

    async def _fail_once_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"expected_ack": b"\x03\x04"}

    mock_mc.commands.send_msg.side_effect = _fail_once_then_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("AABBCC", "retry msg")
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # Should use cached 3.0, NOT linear fallback 0.1.
        assert retry_sleeps[0].args[0] == 3.0


# ===================================================================
# Test 8: unrelated contact still isolated with normalization
# ===================================================================


async def test_unrelated_contact_still_isolated() -> None:
    """Send DM to 'AABBCC' (captures timeout), then send to 'DDEEFF'
    (no timeout).  Verify 'DDEEFF' retry uses linear fallback, not
    'AABBCC's cached timeout."""
    session, mock_mc = _make_tcp_session()

    # Send to AABBCC — captures suggested_timeout.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("AABBCC", "msg to A")

    assert session.diagnostics()["sdk_contact_timeout_count"] == 1

    # Send to DDEEFF — no suggested_timeout, then fail to trigger retry.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x03\x04",
    }
    call_count = 0

    async def _fail_once_then_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"expected_ack": b"\x05\x06"}

    mock_mc.commands.send_msg.side_effect = _fail_once_then_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("DDEEFF", "msg to D")
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # DDEEFF should use linear fallback 0.1, NOT AABBCC's 2.0.
        assert retry_sleeps[0].args[0] == 0.1

    assert "ddeeff" not in session._contact_retry_delays


# ===================================================================
# Test 9: diagnostics exposes count, not keys — normalization dedupes
# ===================================================================


async def test_diagnostics_still_exposes_only_count() -> None:
    """Send DM to 'AABBCC' and 'aabbcc' (same normalized key).  Verify
    sdk_contact_timeout_count == 1 (normalization deduplicates keys)."""
    session, mock_mc = _make_tcp_session()

    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }

    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("AABBCC", "msg 1")

    # Second send to the same normalized key overwrites, not adds.
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x03\x04",
        "suggested_timeout": 3000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("aabbcc", "msg 2")

    # Both normalize to "aabbcc" — single entry, latest value.
    assert session.diagnostics()["sdk_contact_timeout_count"] == 1
    assert session._contact_retry_delays.get("aabbcc") == 3.0


# ===================================================================
# Test 10: channel sends still never consult cache (with normalization)
# ===================================================================


async def test_channel_sends_still_never_consult_cache() -> None:
    """Send channel message; even if a DM timeout is cached for same
    contact, channel retry uses linear 0.1*attempt fallback."""
    session, mock_mc = _make_tcp_session()

    # Cache a DM timeout for "AABBCC" (normalizes to "aabbcc").
    mock_mc.commands.send_msg.return_value = {
        "expected_ack": b"\x01\x02",
        "suggested_timeout": 2000,
    }
    with patch("medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock):
        await session.send_text("AABBCC", "dm msg")

    assert session._contact_retry_delays.get("aabbcc") == 2.0

    # Send a channel message that fails once then succeeds.
    call_count = 0

    async def _fail_once_ok(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    mock_mc.commands.send_chan_msg.side_effect = _fail_once_ok

    with patch(
        "medre.adapters.meshcore.session.asyncio.sleep", new_callable=AsyncMock
    ) as mock_sleep:
        await session.send_text("ignored", "chan msg", channel_index=0)
        retry_sleeps = [
            c
            for c in mock_sleep.call_args_list
            if c.args[0] != session._config.message_delay_seconds
        ]
        assert len(retry_sleeps) >= 1
        # Should use fallback 0.1, NOT cached 2.0.
        assert retry_sleeps[0].args[0] == 0.1

    # DM cache untouched — channel send did not read or write it.
    assert session._contact_retry_delays.get("aabbcc") == 2.0
