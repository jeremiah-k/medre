"""Opt-in Meshtastic hardware endurance checks for the MEDRE adapter boundary."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext

pytestmark = [
    pytest.mark.live,
    pytest.mark.hardware,
    pytest.mark.soak,
    pytest.mark.meshtastic_sdk,
]


def _config_from_env() -> MeshtasticConfig:
    connection_type = os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower()
    if connection_type == "tcp":
        host = os.environ.get("MESHTASTIC_HOST")
        if not host:
            pytest.skip("MESHTASTIC_HOST is required for TCP hardware soak")
        return MeshtasticConfig(
            adapter_id="meshtastic-hardware-soak",
            connection_type="tcp",
            host=host,
            port=int(os.environ.get("MESHTASTIC_PORT", "4403")),
            message_delay_seconds=0,
        )
    if connection_type == "serial":
        serial_port = os.environ.get("MESHTASTIC_SERIAL_PORT")
        if not serial_port:
            pytest.skip("MESHTASTIC_SERIAL_PORT is required for serial hardware soak")
        return MeshtasticConfig(
            adapter_id="meshtastic-hardware-soak",
            connection_type="serial",
            serial_port=serial_port,
            message_delay_seconds=0,
        )
    if connection_type == "ble":
        ble_address = os.environ.get("MESHTASTIC_BLE_ADDRESS")
        if not ble_address:
            pytest.skip("MESHTASTIC_BLE_ADDRESS is required for BLE hardware soak")
        return MeshtasticConfig(
            adapter_id="meshtastic-hardware-soak",
            connection_type="ble",
            ble_address=ble_address,
            message_delay_seconds=0,
        )
    pytest.skip(
        "Set MESHTASTIC_CONNECTION_TYPE to tcp, serial, or ble for hardware soak"
    )


def _context() -> AdapterContext:
    async def publish_inbound(_event: object) -> None:
        return None

    return AdapterContext(
        adapter_id="meshtastic-hardware-soak",
        event_bus=None,
        publish_inbound=publish_inbound,
        logger=logging.getLogger("test.meshtastic.hardware-soak"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def test_repeated_hardware_start_health_stop_cycles() -> None:
    """Exercise repeated real-radio lifecycle without transmitting RF traffic."""
    cycles = int(os.environ.get("MESHTASTIC_SOAK_CYCLES", "10"))
    assert 1 <= cycles <= 100
    config = _config_from_env()
    adapter = MeshtasticAdapter(config)
    ctx = _context()

    for _ in range(cycles):
        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=30.0)
            info = await asyncio.wait_for(adapter.health_check(), timeout=10.0)
            assert info.health == "healthy"
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=15.0)
        stopped = await asyncio.wait_for(adapter.health_check(), timeout=10.0)
        assert stopped.health == "unknown"
