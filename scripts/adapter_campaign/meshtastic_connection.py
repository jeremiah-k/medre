"""One lazy native connection selector for radio peers and Docker lab roles."""

from __future__ import annotations

from typing import Any


def role_connection(settings: dict[str, Any], role: str) -> dict[str, Any]:
    """Select exactly one explicit connection for a gateway or independent peer."""
    connection = {
        key: settings[f"{role}_{key}"]
        for key in ("serial_port", "ble_address", "host", "port")
        if f"{role}_{key}" in settings
    }
    connection_config(connection)
    return connection


def connection_config(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate native connection selection and return matching adapter config."""
    keys = [key for key in ("serial_port", "ble_address", "host") if key in settings]
    if len(keys) != 1:
        raise ValueError(
            "Meshtastic requires exactly one serial_port, ble_address or host"
        )
    key = keys[0]
    value = settings[key]
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Meshtastic connection address must be a nonempty string")
    config: dict[str, Any] = {
        "connection_type": {
            "serial_port": "serial",
            "ble_address": "ble",
            "host": "tcp",
        }[key],
        key: value,
    }
    if "port" in settings:
        port = settings["port"]
        if key != "host" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError(
                "Meshtastic port requires a TCP host and an integer from 1 to 65535"
            )
        config["port"] = port
    return config


def open_interface(settings: dict[str, Any], *, timeout: float) -> Any:
    """Construct the selected SDK interface without loading other transports."""
    config = connection_config(settings)
    if config["connection_type"] == "serial":
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(config["serial_port"], timeout=timeout)
    if config["connection_type"] == "ble":
        from meshtastic.ble_interface import BLEInterface

        return BLEInterface(address=config["ble_address"], timeout=timeout)
    from meshtastic.tcp_interface import TCPInterface

    return TCPInterface(
        hostname=config["host"], portNumber=config.get("port", 4403), timeout=timeout
    )
