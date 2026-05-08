"""Meshtastic adapter configuration.

:class:`MeshtasticConfig` is a frozen dataclass that holds all settings
required to connect to a Meshtastic radio node.  Use
:meth:`MeshtasticConfig.validate` to verify the configuration before
passing it to :class:`MeshtasticAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self

from medre.adapters.meshtastic.errors import MeshtasticConfigError


@dataclass(frozen=True)
class MeshtasticConfig:
    """Immutable configuration for a
    :class:`~medre.adapters.meshtastic.adapter.MeshtasticAdapter`.

    Attributes
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    connection_type:
        Connection mode: ``"fake"``, ``"tcp"``, ``"serial"``, or ``"ble"``.
        Defaults to ``"fake"`` for testing without hardware.
    host:
        Hostname or IP for TCP connections.
    port:
        Port number for TCP connections.
    serial_port:
        Serial device path for serial connections.
    meshnet_name:
        Human-readable meshnet name (informational).
    default_channel:
        Default radio channel index for outbound messages.
    channel_mapping:
        Mapping of channel index to human-readable channel name.
    message_delay_seconds:
        Minimum delay between outbound messages (pacing).
    startup_backlog_suppress_seconds:
        Seconds after start to suppress stale backlog packets.
    sync_timeout_ms:
        Timeout in milliseconds for sync operations.
    """

    adapter_id: str
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    meshnet_name: str = ""
    default_channel: int = 0
    channel_mapping: dict[int, str] = field(default_factory=dict)
    message_delay_seconds: float = 0.5
    startup_backlog_suppress_seconds: float = 5.0
    sync_timeout_ms: int = 30000

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        MeshtasticConfigError
            If any required field is missing or malformed.
        """
        if not self.adapter_id:
            raise MeshtasticConfigError("adapter_id must be non-empty")
        if self.connection_type not in ("fake", "tcp", "serial", "ble"):
            raise MeshtasticConfigError(
                f"connection_type must be one of fake/tcp/serial/ble, "
                f"got {self.connection_type!r}"
            )
        if self.message_delay_seconds < 0:
            raise MeshtasticConfigError(
                f"message_delay_seconds must be >= 0, "
                f"got {self.message_delay_seconds}"
            )
        if self.default_channel < 0:
            raise MeshtasticConfigError(
                f"default_channel must be >= 0, got {self.default_channel}"
            )
        if self.connection_type == "tcp" and not self.host:
            raise MeshtasticConfigError(
                "host is required when connection_type is 'tcp'"
            )
        return self
