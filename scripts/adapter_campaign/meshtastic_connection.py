"""One lazy native connection selector for radio peers and Docker lab roles."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
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


def open_interface(
    settings: dict[str, Any],
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Construct the selected SDK interface without loading other transports."""
    config = connection_config(settings)

    def cancellation_kwargs(factory: Any) -> dict[str, Any]:
        # Older SDK releases and injected factories may not support this yet.
        if cancel_event is None:
            return {}
        try:
            param = inspect.signature(factory).parameters.get("cancelEvent")
        except (TypeError, ValueError):
            return {}
        if param is None or param.kind is inspect.Parameter.POSITIONAL_ONLY:
            return {}
        return {"cancelEvent": cancel_event}

    if config["connection_type"] == "serial":
        from meshtastic.serial_interface import SerialInterface

        return SerialInterface(
            config["serial_port"],
            timeout=timeout,
            **cancellation_kwargs(SerialInterface),
        )
    if config["connection_type"] == "ble":
        from meshtastic.ble_interface import BLEInterface

        return BLEInterface(
            address=config["ble_address"],
            timeout=timeout,
            **cancellation_kwargs(BLEInterface),
        )
    from meshtastic.tcp_interface import TCPInterface

    return TCPInterface(
        hostname=config["host"],
        portNumber=config.get("port", 4403),
        timeout=timeout,
        **cancellation_kwargs(TCPInterface),
    )


async def open_owned_interface(settings: dict[str, Any], *, timeout: float) -> Any:
    """Keep a cancelled peer's late SDK result owned until it can be closed.

    asyncio cancellation does not interrupt an executor thread. Where supported,
    the SDK receives a cooperative event; otherwise we still retire the result
    when the uninterruptible constructor eventually returns.
    """
    abandoned = threading.Event()
    native_done = threading.Event()
    ownership = threading.Lock()
    published: dict[str, Any] = {}
    logger = logging.getLogger(__name__)

    def dispose(interface: Any) -> None:
        try:
            interface.close()
        except Exception:
            logger.warning(
                "Failed to close abandoned Meshtastic campaign peer", exc_info=True
            )

    def build() -> Any:
        try:
            if abandoned.is_set():
                return None
            interface = open_interface(
                settings, timeout=timeout, cancel_event=abandoned
            )
            with ownership:
                if not abandoned.is_set():
                    published["interface"] = interface
                    return interface
            # Native thread still owns an interface the asyncio caller abandoned.
            dispose(interface)
            return None
        finally:
            native_done.set()

    task = asyncio.create_task(asyncio.to_thread(build))
    try:
        # wait() keeps task cancellation from discarding a native thread result.
        await asyncio.wait((task,))
        result = task.result()
        if result is None:
            raise ConnectionError("Meshtastic peer constructor did not return a client")
        return result
    except asyncio.CancelledError:
        with ownership:
            abandoned.set()
        if not native_done.is_set():
            logger.warning(
                "Meshtastic campaign peer construction is still running; "
                "native SDK shutdown may await its connection timeout"
            )

        def retire_late(completed: asyncio.Task[Any]) -> None:
            try:
                completed.result()
            except (Exception, asyncio.CancelledError):
                pass
            with ownership:
                interface = published.pop("interface", None)
            if interface is not None:
                dispose(interface)

        task.add_done_callback(retire_late)
        raise
