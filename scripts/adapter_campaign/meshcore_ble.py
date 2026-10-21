"""Own native BLE resources across connection failures and peer shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


@asynccontextmanager
async def open_meshcore_ble(address: str) -> AsyncIterator[Any]:
    # Keep native SDK loading out of offline campaign planning.
    from bleak import BleakScanner
    from meshcore import MeshCore
    from meshcore.ble_cx import BLEConnection

    device = await BleakScanner.find_device_by_address(address, timeout=10)
    if device is None:
        raise ConnectionError("MeshCore BLE device is not advertising")
    connection = MeshCore(
        BLEConnection(address=address, device=device),
        default_timeout=10,
        auto_reconnect=False,
    )
    # Retain the client before connect: an SDK factory can raise after opening
    # the link, leaving callers without an object they can disconnect.
    try:
        async with asyncio.timeout(30):
            if await connection.connect() is None:
                raise ConnectionError("MeshCore BLE connection was not established")
        yield connection
    finally:
        async with asyncio.timeout(10):
            await connection.disconnect()
