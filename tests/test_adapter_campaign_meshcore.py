"""Native BLE clients stay owned until connection and peer cleanup complete."""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.adapter_campaign.meshcore_ble import open_meshcore_ble


@pytest.fixture
def native_ble(monkeypatch):
    device = object()
    scanner = SimpleNamespace(find_device_by_address=AsyncMock(return_value=device))
    transport = Mock()
    client = SimpleNamespace(
        connect=AsyncMock(return_value="test-board"),
        disconnect=AsyncMock(),
        self_info={"name": "peer"},
        commands=SimpleNamespace(
            send_appstart=AsyncMock(
                return_value=SimpleNamespace(type="self_info", payload={"name": "peer"})
            ),
            get_time=AsyncMock(return_value=SimpleNamespace(payload={"time": 1})),
        ),
    )
    factory = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakScanner=scanner))
    monkeypatch.setitem(
        sys.modules,
        "meshcore",
        SimpleNamespace(
            MeshCore=factory, EventType=SimpleNamespace(SELF_INFO="self_info")
        ),
    )
    monkeypatch.setitem(
        sys.modules, "meshcore.ble_cx", SimpleNamespace(BLEConnection=transport)
    )
    return SimpleNamespace(
        device=device,
        scanner=scanner,
        transport=transport,
        client=client,
        factory=factory,
    )


async def test_ble_uses_live_device_and_closes_after_peer_work(native_ble):
    async with open_meshcore_ble("test-board") as client:
        assert client is native_ble.client
        client.disconnect.assert_not_awaited()
    native_ble.scanner.find_device_by_address.assert_awaited_once_with(
        "test-board", timeout=10
    )
    native_ble.transport.assert_called_once_with(
        address="test-board", device=native_ble.device
    )
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("failure", [ConnectionError("partial connection"), None])
async def test_failed_connect_still_closes_owned_client(native_ble, failure):
    if failure is None:
        native_ble.client.connect.return_value = None
    else:
        native_ble.client.connect.side_effect = failure
    with pytest.raises(ConnectionError):
        async with open_meshcore_ble("test-board"):
            pytest.fail("A failed connection must never become ready")
    native_ble.client.disconnect.assert_awaited_once()


async def test_cancelled_connect_still_closes_owned_client(native_ble):
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def connect():
        started.set()
        await blocked.wait()

    native_ble.client.connect.side_effect = connect

    async def peer():
        async with open_meshcore_ble("test-board"):
            pytest.fail("Cancelled connection must not admit peer work")

    task = asyncio.create_task(peer())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    native_ble.client.disconnect.assert_awaited_once()


async def test_peer_failure_closes_connection(native_ble):
    with pytest.raises(RuntimeError, match="peer work failed"):
        async with open_meshcore_ble("test-board"):
            raise RuntimeError("peer work failed")
    native_ble.client.disconnect.assert_awaited_once()


async def test_missing_advertisement_does_not_construct_client(native_ble):
    native_ble.scanner.find_device_by_address.return_value = None
    with pytest.raises(ConnectionError, match="not advertising"):
        async with open_meshcore_ble("test-board"):
            pytest.fail("Missing board must never become ready")
    native_ble.factory.assert_not_called()


async def test_hardware_cleanup_failure_does_not_qualify_pair(native_ble, tmp_path):
    from scripts.adapter_campaign.lab.hardware import inspect_hardware

    (tmp_path / "hardware.json").write_text(
        json.dumps(
            {"meshcore": {"gateway_ble_address": "gateway", "peer_ble_address": "peer"}}
        )
    )
    for name in ("logs", "profiles", "results"):
        (tmp_path / name).mkdir()
    native_ble.client.disconnect.side_effect = RuntimeError("cleanup failed")
    report = await inspect_hardware(tmp_path)
    assert report["meshcore"]["gateway"]["status"] == "unavailable"
    assert report["meshcore"]["peer"] == {
        "status": "not_attempted",
        "reason": "re_pair_required",
    }
    native_ble.client.commands.get_time.assert_awaited_once()
    native_ble.client.disconnect.assert_awaited_once()
    assert (tmp_path / "results/meshcore-paused.json").is_file()
