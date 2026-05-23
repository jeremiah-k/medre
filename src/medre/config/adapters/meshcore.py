"""MeshCore adapter configuration.

:class:`MeshCoreConfig` is a frozen dataclass that holds all settings
required to connect to a MeshCore radio node.  Use
:meth:`MeshCoreConfig.validate` to verify the configuration before
passing it to :class:`MeshCoreAdapter`.

Validation rules
----------------
- ``adapter_id`` must be non-empty.
- ``connection_type`` must be one of ``"fake"``, ``"tcp"``, ``"serial"``,
  or ``"ble"``.
- Non-fake connection types require their associated field:
  ``"tcp"`` → ``host``, ``"serial"`` → ``serial_port``, ``"ble"``
  (future: will require ``ble_address``).
- ``identity`` and ``pubkey`` are optional; if provided they must be
  non-empty strings.
- ``node_config`` is an opaque dict for future node-specific settings.
  It must not contain keys named ``"private_key"``, ``"secret"``, or
  ``"password"`` — secrets must be provisioned through a secure channel,
  never embedded in configuration metadata.
- ``message_delay_seconds`` ≥ 0, ``default_channel`` ≥ 0,
  ``sync_timeout_ms`` > 0.
- ``max_text_bytes`` ≥ 0, must be ``int`` (``bool`` rejected explicitly).
- ``startup_backlog_suppress_seconds`` ≥ 0, must be ``int`` or ``float``
  (``bool`` rejected explicitly).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Literal, Self

from medre.config.adapters.errors import MeshCoreConfigError

__all__ = ["MeshCoreConfig"]


# Hex-like string: at least one hex character (used for pubkey validation).
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

# Keys that must never appear in node_config — secrets belong in a vault.
_FORBIDDEN_SECRET_KEYS = frozenset({"private_key", "secret", "password"})


@dataclass(frozen=True)
class MeshCoreConfig:
    """Immutable configuration for a
    :class:`~medre.adapters.meshcore.adapter.MeshCoreAdapter`.

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
    serial_baudrate:
        Baud rate for serial connections.  Defaults to 115200.
    ble_address:
        BLE MAC address for BLE connections.  Required when
        *connection_type* is ``"ble"`` (future).
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
    identity:
        Optional MeshCore node identity string (e.g. a node name).
        If provided, must be non-empty.
    pubkey:
        Optional MeshCore public key as a hex string.  If provided,
        must consist of hexadecimal characters only.
    node_config:
        Opaque dict for future node-specific settings.  Must not
        contain secret/private-key fields.
    max_text_bytes:
        Maximum UTF-8 byte budget for the final radio text after
        rendering.  Default: ``512``.  ``0`` means the final text
        renders as an empty string.  Must be a non-negative integer;
        ``bool`` is rejected explicitly.
    """

    adapter_id: str
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    serial_baudrate: int = 115200
    ble_address: str | None = None
    meshnet_name: str = ""
    default_channel: int = 0
    channel_mapping: dict[int, str] = field(default_factory=dict)
    message_delay_seconds: float = 0.5
    startup_backlog_suppress_seconds: float = 5.0
    sync_timeout_ms: int = 30000
    identity: str | None = None
    pubkey: str | None = None
    node_config: dict[str, object] = field(default_factory=dict)
    max_text_bytes: int = 512

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        MeshCoreConfigError
            If any required field is missing or malformed.
        """
        if not self.adapter_id:
            raise MeshCoreConfigError("adapter_id must be non-empty")
        if self.connection_type not in ("fake", "tcp", "serial", "ble"):
            raise MeshCoreConfigError(
                f"connection_type must be one of fake/tcp/serial/ble, "
                f"got {self.connection_type!r}"
            )
        if self.message_delay_seconds < 0:
            raise MeshCoreConfigError(
                f"message_delay_seconds must be >= 0, "
                f"got {self.message_delay_seconds}"
            )
        if self.default_channel < 0:
            raise MeshCoreConfigError(
                f"default_channel must be >= 0, got {self.default_channel}"
            )
        if self.sync_timeout_ms <= 0:
            raise MeshCoreConfigError(
                f"sync_timeout_ms must be > 0, got {self.sync_timeout_ms}"
            )
        if isinstance(self.max_text_bytes, bool):
            raise MeshCoreConfigError("max_text_bytes must be an int, got bool")
        if not isinstance(self.max_text_bytes, int):
            raise MeshCoreConfigError(
                f"max_text_bytes must be an int, got {type(self.max_text_bytes).__name__}"
            )
        if self.max_text_bytes < 0:
            raise MeshCoreConfigError(
                f"max_text_bytes must be >= 0, got {self.max_text_bytes}"
            )

        if isinstance(self.startup_backlog_suppress_seconds, bool):
            raise MeshCoreConfigError(
                "startup_backlog_suppress_seconds must be an int or float, got bool"
            )
        if not isinstance(self.startup_backlog_suppress_seconds, (int, float)):
            raise MeshCoreConfigError(
                f"startup_backlog_suppress_seconds must be an int or float, "
                f"got {type(self.startup_backlog_suppress_seconds).__name__}"
            )
        if not math.isfinite(self.startup_backlog_suppress_seconds):
            raise MeshCoreConfigError(
                "startup_backlog_suppress_seconds must be finite"
            )
        if self.startup_backlog_suppress_seconds < 0:
            raise MeshCoreConfigError(
                f"startup_backlog_suppress_seconds must be >= 0, "
                f"got {self.startup_backlog_suppress_seconds}"
            )

        # Non-fake connection type validation
        if self.connection_type == "tcp" and not self.host:
            raise MeshCoreConfigError("host is required when connection_type is 'tcp'")
        if self.connection_type == "serial" and not self.serial_port:
            raise MeshCoreConfigError(
                "serial_port is required when connection_type is 'serial'"
            )
        if self.connection_type == "ble" and not self.ble_address:
            raise MeshCoreConfigError(
                "ble_address is required when connection_type is 'ble'"
            )

        # Identity / pubkey validation (optional but typed if present)
        if self.identity is not None and not self.identity:
            raise MeshCoreConfigError(
                "identity must be a non-empty string when provided"
            )
        if self.pubkey is not None:
            if not self.pubkey:
                raise MeshCoreConfigError(
                    "pubkey must be a non-empty hex string when provided"
                )
            if not _HEX_RE.match(self.pubkey):
                raise MeshCoreConfigError(
                    f"pubkey must contain only hexadecimal characters, "
                    f"got {self.pubkey!r}"
                )

        # node_config: no embedded secrets
        _forbidden = _FORBIDDEN_SECRET_KEYS & self.node_config.keys()
        if _forbidden:
            raise MeshCoreConfigError(
                f"node_config must not contain secret keys: "
                f"{', '.join(sorted(_forbidden))}. "
                f"Provision secrets through a secure channel, not config metadata."
            )

        return self
