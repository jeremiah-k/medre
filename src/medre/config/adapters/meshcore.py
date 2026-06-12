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
  → ``ble_address``.
- ``port`` is optional; if provided must be ``int`` (not ``bool``)
  between 1 and 65535.
- ``identity`` and ``pubkey`` are optional; if provided they must be
  non-empty strings.
- ``node_config`` is an opaque dict for future node-specific settings.
  It must not contain keys named ``"private_key"``, ``"secret"``, or
  ``"password"`` — secrets must be provisioned through a secure channel,
  never embedded in configuration metadata.
- ``ble_pin`` is optional; if provided must be a non-empty string.
  It is a potentially sensitive value and must never be exposed in
  diagnostics, logs, or JSON output.
- ``message_delay_seconds`` must be a non-negative finite number
  (≥ 0, finite), ``default_channel`` ≥ 0.
- ``max_text_bytes`` ≥ 0, must be ``int`` (``bool`` rejected explicitly).
- ``meshcore_relay_prefix`` must be a string (``bool`` rejected explicitly).
  Default ``""`` means no prefix is prepended.  When non-empty, the
  string is a template for :func:`~medre.core.rendering.attribution.format_relay_prefix`.
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
        Port number for TCP connections.  Optional; when ``None`` and
        ``connection_type="tcp"``, the session uses TCP port 4000.
        Must be between 1 and 65535 when provided.
    serial_port:
        Serial device path for serial connections.
    serial_baudrate:
        Baud rate for serial connections.  Defaults to 115200.
    ble_address:
        BLE MAC address for BLE connections.  Required when
        *connection_type* is ``"ble"``.
    ble_pin:
        Optional BLE pairing PIN.  If provided, must be a non-empty
        string.  Passed to the SDK's ``create_ble(pin=...)`` for
        programmatic pairing.  This is a sensitive value — it is
        never exposed in diagnostics, logs, or JSON output.
        Host-level pairing via ``bluetoothctl`` remains the
        recommended path; this field exists for automated/headless
        deployments.
    meshnet_name:
        Human-readable meshnet name (informational).
    default_channel:
        Default radio channel index for outbound messages.
    message_delay_seconds:
        Minimum delay between outbound messages (pacing).
        Must be a non-negative finite number; ``nan`` and ``inf``
        are rejected.
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
    meshcore_relay_prefix:
        Optional relay prefix template prepended to outbound text
        before byte-budget truncation.  Default: ``""`` (no prefix).
        When non-empty, the value is passed to
        :func:`~medre.core.rendering.attribution.format_relay_prefix`
        along with attribution extracted from the source event.
        The rendered prefix counts toward ``max_text_bytes``.
        Must be a string; ``bool`` is rejected explicitly.
    """

    adapter_id: str
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    serial_baudrate: int = 115200
    ble_address: str | None = None
    ble_pin: str | None = None
    meshnet_name: str = ""
    origin_label: str = ""
    default_channel: int = 0
    message_delay_seconds: float = 0.5
    identity: str | None = None
    pubkey: str | None = None
    node_config: dict[str, object] = field(default_factory=dict)
    max_text_bytes: int = 512
    meshcore_relay_prefix: str = ""

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
        # --- numeric fields ---
        if isinstance(self.message_delay_seconds, bool):
            raise MeshCoreConfigError(
                "message_delay_seconds must be int or float, got bool"
            )
        if not isinstance(self.message_delay_seconds, (int, float)):
            raise MeshCoreConfigError(
                f"message_delay_seconds must be int or float, "
                f"got {type(self.message_delay_seconds).__name__}"
            )
        if not math.isfinite(self.message_delay_seconds):
            raise MeshCoreConfigError("message_delay_seconds must be finite")
        if self.message_delay_seconds < 0:
            raise MeshCoreConfigError(
                f"message_delay_seconds must be >= 0, "
                f"got {self.message_delay_seconds}"
            )
        if isinstance(self.default_channel, bool):
            raise MeshCoreConfigError("default_channel must be an int, got bool")
        if not isinstance(self.default_channel, int):
            raise MeshCoreConfigError(
                f"default_channel must be an int, got {type(self.default_channel).__name__}"
            )
        if self.default_channel < 0:
            raise MeshCoreConfigError(
                f"default_channel must be >= 0, got {self.default_channel}"
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

        # meshcore_relay_prefix: must be str, bool rejected
        if isinstance(self.meshcore_relay_prefix, bool):
            raise MeshCoreConfigError("meshcore_relay_prefix must be a str, got bool")
        if not isinstance(self.meshcore_relay_prefix, str):
            raise MeshCoreConfigError(
                f"meshcore_relay_prefix must be a str, "
                f"got {type(self.meshcore_relay_prefix).__name__}"
            )

        # --- origin_label ---
        if isinstance(self.origin_label, bool):
            raise MeshCoreConfigError("origin_label must be a str, got bool")
        if not isinstance(self.origin_label, str):
            raise MeshCoreConfigError(
                f"origin_label must be a str, "
                f"got {type(self.origin_label).__name__}"
            )

        # Non-fake connection type validation
        if self.connection_type == "tcp" and not self.host:
            raise MeshCoreConfigError("host is required when connection_type is 'tcp'")
        if self.port is not None:
            if isinstance(self.port, bool):
                raise MeshCoreConfigError("port must be an int, got bool")
            if not isinstance(self.port, int):
                raise MeshCoreConfigError(
                    f"port must be an int, got {type(self.port).__name__}"
                )
            if self.port < 1 or self.port > 65535:
                raise MeshCoreConfigError(
                    f"port must be between 1 and 65535, got {self.port}"
                )
        if self.connection_type == "serial" and not self.serial_port:
            raise MeshCoreConfigError(
                "serial_port is required when connection_type is 'serial'"
            )
        if self.connection_type == "serial":
            if not isinstance(self.serial_baudrate, int) or isinstance(
                self.serial_baudrate, bool
            ):
                raise MeshCoreConfigError(
                    f"serial_baudrate must be an integer, got {type(self.serial_baudrate).__name__}"
                )
            if self.serial_baudrate <= 0:
                raise MeshCoreConfigError(
                    f"serial_baudrate must be > 0, got {self.serial_baudrate}"
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

        # ble_pin validation (optional, but must be a non-empty string if provided)
        if self.ble_pin is not None:
            if not isinstance(self.ble_pin, str) or not self.ble_pin:
                raise MeshCoreConfigError(
                    "ble_pin must be a non-empty string when provided"
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
