"""Meshtastic adapter configuration.

:class:`MeshtasticConfig` is a frozen dataclass that holds all settings
required to connect to a Meshtastic radio node.  Use
:meth:`MeshtasticConfig.validate` to verify the configuration before
passing it to :class:`MeshtasticAdapter`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Self

from medre.config.adapters.errors import MeshtasticConfigError

__all__ = ["MeshtasticConfig"]


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
    default_channel:
        Default radio channel index for outbound messages.
    channel_mapping:
        Mapping of channel index to human-readable channel name.
        **This is a display-label map, not a relay allowlist.** Packets
        on unmapped channel indices are still classified normally (the
        packet classifier does not gate on channel membership). If a
        channel-allowlist gate is needed in the future, introduce a
        separate ``allowed_channels`` field rather than overloading this
        one.
    message_delay_seconds:
        Minimum delay between outbound messages (pacing).
    startup_backlog_suppress_seconds:
        Seconds after start to suppress stale backlog packets.
    sync_timeout_ms:
        Timeout in milliseconds for sync operations.
    radio_relay_prefix:
        Template string prepended to messages relayed **from** Matrix
        **to** Meshtastic radio.  Uses Python ``str.format()`` syntax
        with generic variables: ``{sender}``, ``{sender_short}``,
        ``{sender_id}``, ``{origin_label}``, ``{platform}``,
        ``{channel}``, ``{route_id}``.
        Default: ``"{sender_short}: "``.

        Example: ``"{sender_short}: "``
    mmrelay_compatibility:
        When ``True``, the Matrix renderer embeds mmrelay-compatible
        Meshtastic metadata into the Matrix event content payload.  This
        allows downstream consumers expecting mmrelay's ``meshtastic_*``
        fields to interoperate with medre-relayed messages.  Default:
        ``False``.
    max_text_bytes:
        Maximum UTF-8 byte budget for the final radio text after
        rendering.  Applied after all prefix, reply, and reaction
        formatting is complete.  Default: ``227``, informed by the
        MMRelay ``DEFAULT_MESSAGE_TRUNCATE_BYTES`` constant.

        **Relation overhead tradeoff.** The Meshtastic-Android
        ``DATA_PAYLOAD_LEN = 233`` applies to the *entire encoded*
        ``Data`` protobuf — not just the text payload.  When
        ``reply_id`` (field 7, up to 6 encoded bytes) and ``emoji``
        (field 8, up to 2 encoded bytes) are present, the combined
        overhead is up to ~8 bytes.  At the default 227-byte text
        budget this leaves marginal headroom for the worst-case
        relation-structured send.  Operators tuning for
        relation-heavy workloads should consider lowering this to
        ~219-225.  The field is per-adapter so different radios can
        use different budgets.  ``0`` means the final text renders
        as an empty string.  Env override:
        ``MEDRE_ADAPTER__<TOKEN>__MAX_TEXT_BYTES``.
    queue_send_max_attempts:
        Maximum number of send attempts per queued item (first attempt
        + retries).  When a transient send failure occurs and the
        attempt count is below this limit the item is requeued to the
        front of the queue for immediate retry.  When attempts are
        exhausted the item is dropped and counted as exhausted.
        ``bool``, non-``int``, and ``<= 0`` values are invalid.
        Default: ``3``.
    outbound_mode:
        Controls whether outbound radio sends are enabled.

        * ``"enabled"`` (default) — outbound messages are enqueued and
          delivered normally.
        * ``"listen_only"`` — outbound radio sends are suppressed before
          queue enqueue.  The adapter still receives and classifies
          inbound packets.  Suppressed deliveries raise
          :class:`~medre.core.contracts.adapter.AdapterPermanentError`
          with a stable prefix ``"outbound suppressed: listen_only mode"``
          so that delivery evidence records a permanent failure with
          ``failure_kind="adapter_permanent"`` and a derivable
          ``failure_kind_detail="meshtastic_outbound_suppressed"``.

        Invalid values are rejected by :meth:`validate`.
    """

    adapter_id: str
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
    host: str | None = None
    port: int | None = None
    serial_port: str | None = None
    ble_address: str | None = None
    origin_label: str = ""
    default_channel: int = 0
    channel_mapping: dict[int, str] = field(default_factory=dict)
    message_delay_seconds: float = 0.5
    startup_backlog_suppress_seconds: float = 5.0
    sync_timeout_ms: int = 30000
    radio_relay_prefix: str = "{sender_short}: "
    mmrelay_compatibility: bool = False
    max_text_bytes: int = 227
    queue_send_max_attempts: int = 3
    outbound_mode: Literal["enabled", "listen_only"] = "enabled"

    # Packet routing configuration (configurable classification policy)
    encrypted_action: Literal["drop", "deferred"] = "drop"
    chat_portnums: frozenset[str] = field(default_factory=frozenset)
    disabled_portnums: frozenset[str] = field(default_factory=frozenset)
    detection_sensor_relay: bool = False

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
        if isinstance(self.max_text_bytes, bool):
            raise MeshtasticConfigError("max_text_bytes must be an int, got bool")
        if not isinstance(self.max_text_bytes, int):
            raise MeshtasticConfigError(
                f"max_text_bytes must be an int, got {type(self.max_text_bytes).__name__}"
            )
        if self.max_text_bytes < 0:
            raise MeshtasticConfigError(
                f"max_text_bytes must be >= 0, got {self.max_text_bytes}"
            )
        if isinstance(self.startup_backlog_suppress_seconds, bool):
            raise MeshtasticConfigError(
                "startup_backlog_suppress_seconds must be an int or float, got bool"
            )
        if not isinstance(self.startup_backlog_suppress_seconds, (int, float)):
            raise MeshtasticConfigError(
                f"startup_backlog_suppress_seconds must be an int or float, "
                f"got {type(self.startup_backlog_suppress_seconds).__name__}"
            )
        if not math.isfinite(self.startup_backlog_suppress_seconds):
            raise MeshtasticConfigError(
                "startup_backlog_suppress_seconds must be finite"
            )
        if self.startup_backlog_suppress_seconds < 0:
            raise MeshtasticConfigError(
                f"startup_backlog_suppress_seconds must be >= 0, "
                f"got {self.startup_backlog_suppress_seconds}"
            )
        if isinstance(self.queue_send_max_attempts, bool):
            raise MeshtasticConfigError(
                "queue_send_max_attempts must be an int, got bool"
            )
        if not isinstance(self.queue_send_max_attempts, int):
            raise MeshtasticConfigError(
                f"queue_send_max_attempts must be an int, "
                f"got {type(self.queue_send_max_attempts).__name__}"
            )
        if self.queue_send_max_attempts <= 0:
            raise MeshtasticConfigError(
                f"queue_send_max_attempts must be > 0, "
                f"got {self.queue_send_max_attempts}"
            )
        if self.outbound_mode not in ("enabled", "listen_only"):
            raise MeshtasticConfigError(
                f"outbound_mode must be one of enabled/listen_only, "
                f"got {self.outbound_mode!r}"
            )
        if self.encrypted_action not in ("drop", "deferred"):
            raise MeshtasticConfigError(
                f"encrypted_action must be one of drop/deferred, "
                f"got {self.encrypted_action!r}"
            )
        if not isinstance(self.chat_portnums, frozenset):
            raise MeshtasticConfigError(
                f"chat_portnums must be a frozenset, "
                f"got {type(self.chat_portnums).__name__}"
            )
        if self.chat_portnums and not all(
            isinstance(p, str) for p in self.chat_portnums
        ):
            raise MeshtasticConfigError("chat_portnums must contain only strings")
        if not isinstance(self.disabled_portnums, frozenset):
            raise MeshtasticConfigError(
                f"disabled_portnums must be a frozenset, "
                f"got {type(self.disabled_portnums).__name__}"
            )
        if self.disabled_portnums and not all(
            isinstance(p, str) for p in self.disabled_portnums
        ):
            raise MeshtasticConfigError("disabled_portnums must contain only strings")
        if not isinstance(self.detection_sensor_relay, bool):
            raise MeshtasticConfigError(
                f"detection_sensor_relay must be a bool, "
                f"got {type(self.detection_sensor_relay).__name__}"
            )
        # --- origin_label ---
        if isinstance(self.origin_label, bool):
            raise MeshtasticConfigError("origin_label must be a str, got bool")
        if not isinstance(self.origin_label, str):
            raise MeshtasticConfigError(
                f"origin_label must be a str, "
                f"got {type(self.origin_label).__name__}"
            )
        # --- radio_relay_prefix ---
        if isinstance(self.radio_relay_prefix, bool):
            raise MeshtasticConfigError("radio_relay_prefix must be a str, got bool")
        if not isinstance(self.radio_relay_prefix, str):
            raise MeshtasticConfigError(
                f"radio_relay_prefix must be a str, "
                f"got {type(self.radio_relay_prefix).__name__}"
            )
        return self
