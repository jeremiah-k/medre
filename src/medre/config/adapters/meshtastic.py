"""Meshtastic adapter configuration.

:class:`MeshtasticConfig` is a frozen dataclass that holds all settings
required to connect to a Meshtastic radio node.  Use
:meth:`MeshtasticConfig.validate` to verify the configuration before
passing it to :class:`MeshtasticAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self

from medre.config.adapters.errors import MeshtasticConfigError


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
    ble_address:
        BLE MAC address for BLE connections.  Required when
        *connection_type* is ``"ble"``.
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
    matrix_relay_prefix:
        Template string prepended to messages relayed **from** Meshtastic
        **to** Matrix.  Uses Python ``str.format()`` syntax with variables:
        ``{longname}``, ``{shortname}``, ``{meshnet_name}``, ``{from_id}``.
        Default: ``"[{longname}/{meshnet_name}]: "``.
        Matches mmrelay's ``DEFAULT_MATRIX_PREFIX = "[{long}/{mesh}]: "``.

        Example: ``"[{longname}/{meshnet_name}]: "``
    radio_relay_prefix:
        Template string prepended to messages relayed **from** Matrix
        **to** Meshtastic radio.  Uses Python ``str.format()`` syntax with
        variables: ``{longname}``, ``{shortname}``, ``{shortname5}``,
        ``{meshnet_name}``, ``{from_id}``.  ``{shortname5}`` resolves to
        the first 5 characters of ``{shortname}`` (or ``{from_id}`` if
        shortname is empty).
        Default: ``"{shortname5}[M]: "``.
        Matches mmrelay's ``DEFAULT_MESHTASTIC_PREFIX = "{display5}[M]: "``.

        Example: ``"{shortname5}[M]: "``
    mmrelay_compatibility:
        When ``True``, the Matrix renderer embeds mmrelay-compatible
        Meshtastic metadata into the Matrix event content payload.  This
        allows downstream consumers expecting mmrelay's ``meshtastic_*``
        fields to interoperate with medre-relayed messages.  Default:
        ``False``.
    """

    adapter_id: str
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    ble_address: str | None = None
    meshnet_name: str = ""
    default_channel: int = 0
    channel_mapping: dict[int, str] = field(default_factory=dict)
    message_delay_seconds: float = 0.5
    startup_backlog_suppress_seconds: float = 5.0
    sync_timeout_ms: int = 30000
    matrix_relay_prefix: str = "[{longname}/{meshnet_name}]: "
    radio_relay_prefix: str = "{shortname5}[M]: "
    mmrelay_compatibility: bool = False

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
        if self.connection_type == "serial" and not self.serial_port:
            raise MeshtasticConfigError(
                "serial_port is required when connection_type is 'serial'"
            )
        if self.connection_type == "ble" and not self.ble_address:
            raise MeshtasticConfigError(
                "ble_address is required when connection_type is 'ble'"
            )
        return self
