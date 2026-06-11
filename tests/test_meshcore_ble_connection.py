"""Tests for MeshCoreSession BLE connection helpers, None-retry, and
stale SDK cleanup.

Covers:
- ``_create_ble_with_retries``: None returns are retried with sleep + re-scan
- ``_create_ble_with_retries``: exceptions interleaved with None returns
- ``_create_ble_with_retries``: final exception wrapping/chaining
- ``_cleanup_stale_sdk``: unsubscription + disconnect before reconnect
- ``_cleanup_stale_sdk``: cleanup failures don't block reconnect
- ``_message_callback`` survives reconnect cleanup
- Retry order: sleep → stale cleanup → re-scan between attempts
- Diagnostics last_error set on BLE failure without leaking ble_pin
- No fixed sleeps in tests (``asyncio.sleep`` is patched)
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.meshcore.errors import MeshCoreConnectionError
from medre.adapters.meshcore.session import MeshCoreSession
from medre.config.adapters.meshcore import MeshCoreConfig
from tests.helpers.meshcore_session import (
    build_mock_meshcore_module,
)


def _make_config(**overrides) -> MeshCoreConfig:
    defaults = dict(adapter_id="ble-test")
    defaults.update(overrides)
    return MeshCoreConfig(**defaults)


def _make_ble_session(**config_overrides) -> MeshCoreSession:
    config = _make_config(
        connection_type="ble",
        ble_address="AA:BB:CC:DD:EE:FF",
        **config_overrides,
    )
    return MeshCoreSession(config, "ble-test-session")


# ===================================================================
# _create_ble_with_retries: None returns are retried
# ===================================================================


async def test_none_twice_then_succeeds() -> None:
    """create_ble returns None twice then succeeds on 3rd attempt.

    Expect 3 create_ble calls, 2 sleeps, and 2 re-scans.
    """
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()
    sleep_calls: list[float] = []

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep") as mock_sleep,
        patch.object(session, "_find_ble_device", return_value=None) as mock_find,
    ):
        mock_sleep.side_effect = lambda s: sleep_calls.append(s)

        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    assert call_count == 3
    # 2 sleeps between 3 attempts.
    assert len(sleep_calls) == 2
    assert all(s == 2.0 for s in sleep_calls)
    # 2 re-scans between attempts.
    assert mock_find.call_count == 2


async def test_none_all_three_raises() -> None:
    """create_ble returns None 3 times → MeshCoreConnectionError."""
    mock_mc, _ = build_mock_meshcore_module()

    async def _create_ble(**kwargs):
        return None

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
    ):
        with pytest.raises(MeshCoreConnectionError, match="3 BLE attempt") as exc_info:
            await session._create_ble_with_retries(mock_mc, "AA:BB:CC:DD:EE:FF", None)

    assert "create_ble returned None" in str(exc_info.value)


async def test_none_then_exception_then_succeeds() -> None:
    """create_ble returns None, then raises, then succeeds.

    All failure modes are retried; 3rd attempt succeeds.
    """
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return None
        if call_count == 2:
            raise OSError("BLE adapter busy")
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    assert call_count == 3


async def test_exception_on_final_attempt_raises_meshcore_error() -> None:
    """create_ble raises on final attempt → MeshCoreConnectionError with reason."""
    mock_mc, _ = build_mock_meshcore_module()

    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None
        raise OSError("BLE adapter crashed")

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
    ):
        with pytest.raises(MeshCoreConnectionError, match="3 attempt") as exc_info:
            await session._create_ble_with_retries(mock_mc, "AA:BB:CC:DD:EE:FF", None)

    msg = str(exc_info.value)
    assert "BLE adapter crashed" in msg
    # Wave 1: final exception is chained from the original OSError.
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == "BLE adapter crashed"


# ===================================================================
# _cleanup_stale_sdk: stale SDK cleanup before reconnect
# ===================================================================


async def test_noop_when_meshcore_none() -> None:
    """When _meshcore is None, cleanup is a no-op."""
    session = _make_ble_session()
    assert session._meshcore is None

    await session._cleanup_stale_sdk()

    assert session._meshcore is None
    assert len(session._subscriptions) == 0


async def test_unsubscribes_and_disconnects() -> None:
    """Existing subscriptions are unsubscribed, client disconnected."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    session._meshcore = mock_inst

    sub1 = MagicMock()
    sub2 = MagicMock()
    session._subscriptions = [sub1, sub2]

    await session._cleanup_stale_sdk()

    assert session._meshcore is None
    assert len(session._subscriptions) == 0
    mock_inst.unsubscribe.assert_any_call(sub1)
    mock_inst.unsubscribe.assert_any_call(sub2)
    assert mock_inst.unsubscribe.call_count == 2
    mock_inst.disconnect.assert_awaited_once()


async def test_cleanup_failure_doesnt_prevent_reconnect() -> None:
    """Unsubscribe/disconnect errors are logged but don't block."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    session._meshcore = mock_inst

    # unsubscribe and disconnect both raise.
    mock_inst.unsubscribe.side_effect = RuntimeError("unsub failed")
    mock_inst.disconnect.side_effect = OSError("disconnect failed")

    session._subscriptions = [MagicMock()]

    # Should NOT raise.
    await session._cleanup_stale_sdk()

    assert session._meshcore is None
    assert len(session._subscriptions) == 0


async def test_message_callback_survives_cleanup() -> None:
    """_cleanup_stale_sdk does NOT clear _message_callback."""
    session = _make_ble_session()

    def cb(pkt):
        return None

    session._message_callback = cb

    mock_mc, mock_inst = build_mock_meshcore_module()
    session._meshcore = mock_inst
    session._subscriptions = [MagicMock()]

    await session._cleanup_stale_sdk()

    assert session._message_callback is cb


async def test_stop_requested_not_set() -> None:
    """_cleanup_stale_sdk does NOT set _stop_requested."""
    session = _make_ble_session()

    mock_mc, mock_inst = build_mock_meshcore_module()
    session._meshcore = mock_inst
    session._subscriptions = []

    await session._cleanup_stale_sdk()

    assert session._stop_requested is False


async def test_stale_auto_fetch_stopped_before_disconnect() -> None:
    """stop_auto_message_fetching is awaited before disconnect on stale client."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    session._meshcore = mock_inst
    session._subscriptions = [MagicMock()]

    call_order: list[str] = []

    async def _stop_fetch():
        call_order.append("stop_fetch")

    async def _disconnect():
        call_order.append("disconnect")

    mock_inst.stop_auto_message_fetching = AsyncMock(side_effect=_stop_fetch)
    mock_inst.disconnect = AsyncMock(side_effect=_disconnect)

    await session._cleanup_stale_sdk()

    assert call_order == ["stop_fetch", "disconnect"]
    assert session._meshcore is None
    assert len(session._subscriptions) == 0


async def test_auto_fetch_timeout_does_not_block_disconnect() -> None:
    """stop_auto_message_fetching TimeoutError does not prevent disconnect."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    session._meshcore = mock_inst
    session._subscriptions = [MagicMock()]

    mock_inst.stop_auto_message_fetching = AsyncMock(side_effect=asyncio.TimeoutError)

    # Should NOT raise.
    await session._cleanup_stale_sdk()

    mock_inst.disconnect.assert_awaited_once()
    assert session._meshcore is None
    assert len(session._subscriptions) == 0


async def test_auto_fetch_error_does_not_block_disconnect() -> None:
    """stop_auto_message_fetching generic error does not prevent disconnect."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    session._meshcore = mock_inst
    session._subscriptions = [MagicMock()]

    mock_inst.stop_auto_message_fetching = AsyncMock(
        side_effect=RuntimeError("fetch crash")
    )

    # Should NOT raise.
    await session._cleanup_stale_sdk()

    mock_inst.disconnect.assert_awaited_once()
    assert session._meshcore is None
    assert len(session._subscriptions) == 0


# ===================================================================
# Full BLE reconnect flow: stale SDK cleaned up before _connect_real
# ===================================================================


async def test_reconnect_cleans_stale_then_connects() -> None:
    """_connect_real cleans stale SDK before creating new BLE client."""
    mock_mc, mock_inst = build_mock_meshcore_module()
    # First client (stale) — track its cleanup.
    stale_inst = AsyncMock()
    stale_inst.disconnect = AsyncMock()
    stale_inst.unsubscribe = MagicMock()
    stale_sub = MagicMock()

    session = _make_ble_session()

    # Simulate stale state.
    session._meshcore = stale_inst
    session._subscriptions = [stale_sub]

    def cb(pkt):
        return None

    session._message_callback = cb

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
        patch.object(session, "_disconnect_stale_ble_client"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
    ):
        await session._connect_real()

    # Stale client was cleaned up.
    stale_inst.unsubscribe.assert_called_with(stale_sub)
    stale_inst.disconnect.assert_awaited_once()

    # New client is in place.
    assert session._meshcore is mock_inst
    assert session.connected is True

    # Callback survived.
    assert session._message_callback is cb

    await session.stop()


async def test_first_connect_skips_stale_cleanup() -> None:
    """On first connect, _meshcore is None so _cleanup_stale_sdk is a no-op."""
    mock_mc, mock_inst = build_mock_meshcore_module()

    session = _make_ble_session()
    assert session._meshcore is None

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
        patch.object(session, "_disconnect_stale_ble_client"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
    ):
        await session._connect_real()

    assert session._meshcore is mock_inst
    assert session.connected is True

    await session.stop()


# ===================================================================
# _create_ble_with_retries: re-scan between attempts
# ===================================================================


async def test_rescan_called_between_attempts() -> None:
    """_find_ble_device is called between each retry attempt."""
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    scan_results = [MagicMock(name="device1"), MagicMock(name="device2")]

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(
            session, "_find_ble_device", side_effect=scan_results
        ) as mock_find,
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    # 2 re-scans (between 3 attempts).
    assert mock_find.call_count == 2
    mock_find.assert_any_call("AA:BB:CC:DD:EE:FF")


async def test_rescan_passed_to_next_create_ble() -> None:
    """Re-scanned BLEDevice is passed to subsequent create_ble calls."""
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0
    devices_seen: list[object | None] = []

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        devices_seen.append(kwargs.get("device"))
        if call_count < 2:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()
    rescanned_device = MagicMock(name="rescanned_device")

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=rescanned_device),
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    # First call: device=None (original), Second call: device=rescanned_device.
    assert devices_seen[0] is None
    assert devices_seen[1] is rescanned_device


# ===================================================================
# _create_ble_with_retries: stale BlueZ cleanup between attempts
# ===================================================================


async def test_stale_cleanup_called_between_retries() -> None:
    """_disconnect_stale_ble_client is called between each retry attempt."""
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch.object(session, "_disconnect_stale_ble_client") as mock_stale,
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    # Called twice: between attempt 1→2 and attempt 2→3.
    assert mock_stale.call_count == 2
    mock_stale.assert_any_call("AA:BB:CC:DD:EE:FF")


async def test_cleanup_failure_does_not_block_retry() -> None:
    """_disconnect_stale_ble_client raising does not prevent next create_ble."""
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch.object(
            session,
            "_disconnect_stale_ble_client",
            side_effect=RuntimeError("BlueZ cleanup failed"),
        ),
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    assert call_count == 2


async def test_mixed_none_exception_reports_final_reason() -> None:
    """Attempt 1 returns None, attempt 2 raises, attempt 3 returns None.

    Final error reports the last reason (create_ble returned None)
    and includes attempt count.
    """
    mock_mc, _ = build_mock_meshcore_module()
    call_count = 0

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count in (1, 3):
            return None
        raise OSError("BLE timeout")

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
    ):
        with pytest.raises(MeshCoreConnectionError) as exc_info:
            await session._create_ble_with_retries(mock_mc, "AA:BB:CC:DD:EE:FF", None)

    msg = str(exc_info.value)
    assert "3 BLE attempt" in msg
    assert "create_ble returned None" in msg


async def test_ble_pin_not_leaked_in_error() -> None:
    """When ble_pin is set, MeshCoreConnectionError does not contain the PIN."""
    mock_mc, _ = build_mock_meshcore_module()
    pin = "123456"

    async def _create_ble(**kwargs):
        raise OSError(f"Auth failed with pin={pin}")

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session(ble_pin=pin)

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
    ):
        with pytest.raises(MeshCoreConnectionError) as exc_info:
            await session._create_ble_with_retries(
                mock_mc, "AA:BB:CC:DD:EE:FF", None, pin=pin
            )

    msg = str(exc_info.value)
    assert pin not in msg
    assert "redacted" in msg
    # Original exception is chained (verify type without exposing secret).
    assert isinstance(exc_info.value.__cause__, OSError)


async def test_diagnostics_last_error_set_on_ble_failure() -> None:
    """When all BLE attempts fail via start(), diagnostics.last_error is set
    and does not leak ble_pin.
    """
    mock_mc, _ = build_mock_meshcore_module()
    pin = "999999"

    async def _create_ble(**kwargs):
        raise OSError(f"Auth failed with pin={pin}")

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session(ble_pin=pin)

    async def _noop_cb(pkt):
        pass

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch.object(session, "_disconnect_stale_ble_client"),
    ):
        with pytest.raises(MeshCoreConnectionError):
            await session.start(_noop_cb)

    assert session.last_error is not None
    assert pin not in session.last_error
    assert "redacted" in session.last_error


async def test_diagnostics_last_error_non_pin_ble_failure() -> None:
    """When BLE fails without a pin, diagnostics.last_error contains reason."""
    mock_mc, _ = build_mock_meshcore_module()

    async def _create_ble(**kwargs):
        raise OSError("BLE adapter crashed")

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()

    async def _noop_cb(pkt):
        pass

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.HAS_MESHCORE", True),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", return_value=None),
        patch.object(session, "_disconnect_stale_ble_client"),
    ):
        with pytest.raises(MeshCoreConnectionError):
            await session.start(_noop_cb)

    assert session.last_error is not None
    assert "BLE adapter crashed" in session.last_error
    """After stale BlueZ cleanup, re-scanned device is passed to next create_ble.

    Verifies the full between-attempt sequence: sleep → stale cleanup → re-scan.
    """
    mock_mc, mock_inst = build_mock_meshcore_module()
    call_count = 0
    devices_seen: list[object | None] = []

    async def _create_ble(**kwargs):
        nonlocal call_count
        call_count += 1
        devices_seen.append(kwargs.get("device"))
        if call_count < 3:
            return None
        return mock_inst

    mock_mc.MeshCore.create_ble = _create_ble

    session = _make_ble_session()
    rescanned_device = MagicMock(name="rescanned_device")

    stale_call_order: list[str] = []

    async def _mock_find(addr):
        stale_call_order.append("find")
        return rescanned_device

    async def _mock_stale(addr):
        stale_call_order.append("stale_cleanup")

    with (
        patch.dict(sys.modules, {"meshcore": mock_mc}),
        patch("medre.adapters.meshcore.session.asyncio.sleep"),
        patch.object(session, "_find_ble_device", side_effect=_mock_find),
        patch.object(session, "_disconnect_stale_ble_client", side_effect=_mock_stale),
    ):
        result = await session._create_ble_with_retries(
            mock_mc, "AA:BB:CC:DD:EE:FF", None
        )

    assert result is mock_inst
    # First call: device=None (original), later calls: device=rescanned_device.
    assert devices_seen[0] is None
    assert devices_seen[1] is rescanned_device
    assert devices_seen[2] is rescanned_device
    # Between attempts, stale cleanup happens before re-scan.
    assert stale_call_order == [
        "stale_cleanup",
        "find",
        "stale_cleanup",
        "find",
    ]
