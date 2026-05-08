"""Structured metadata namespaces for canonical events.

Every :class:`CanonicalEvent` carries an :class:`EventMetadata` instance
that groups domain-specific metadata into typed sub-dataclasses:

* :class:`TransportMetadata` – protocol, gateway, encryption state.
* :class:`RoutingMetadata` – matched routes, fanout groups.
* :class:`RadioMetadata` – RF-layer metrics (SNR, RSSI, frequency).
* :class:`TelemetryMetadata` – arbitrary numeric / scalar metrics.
* :class:`NativeMetadata` – adapter-specific opaque data.

Two enums control how metadata is serialised and what is retained:

* :class:`MetadataEmbeddingMode` – controls verbosity of embedded metadata.
* :class:`PrivacyMode` – controls PII stripping behaviour.
"""

from __future__ import annotations

import msgspec
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MetadataEmbeddingMode(Enum):
    """Controls how much metadata is embedded when serialising an event.

    Attributes
    ----------
    OFF:
        No metadata is included.
    MINIMAL:
        Only fields required for routing / delivery are kept.
    SAFE:
        All non-sensitive fields are kept.
    FULL:
        Everything, including potentially sensitive data, is retained.
    """

    OFF = "off"
    MINIMAL = "minimal"
    SAFE = "safe"
    FULL = "full"


class PrivacyMode(Enum):
    """Controls PII stripping behaviour for event metadata.

    Attributes
    ----------
    STANDARD:
        Default – retain standard identifiers.
    ANONYMISED:
        Strip user-identifiable fields, keep operational data.
    STRIPPED:
        Remove all identifying information, keep only metrics.
    """

    STANDARD = "standard"
    ANONYMISED = "anonymised"
    STRIPPED = "stripped"


# ---------------------------------------------------------------------------
# Sub-dataclasses
# ---------------------------------------------------------------------------


class TransportMetadata(msgspec.Struct, frozen=True):
    """Metadata describing the transport layer that carried the event.

    Attributes
    ----------
    protocol:
        Transport protocol name (e.g. ``"mqtt"``, ``"tcp"``, ``"http"``).
    substrate:
        Physical / link-layer substrate (e.g. ``"lorawan"``, ``"wifi"``).
    gateway_id:
        Identifier of the gateway node that relayed the event.
    delivery_method:
        How the message was delivered (e.g. ``"direct"``, ``"store_forward"``).
    delivery_confirmed:
        Whether the transport confirmed successful delivery.
    transport_encrypted:
        Whether the transport layer provided encryption.
    signature_valid:
        Whether a digital signature was verified.
    propagation_state:
        Current propagation state as a transport-specific string.
    """

    protocol: str | None = None
    substrate: str | None = None
    gateway_id: str | None = None
    delivery_method: str | None = None
    delivery_confirmed: bool | None = None
    transport_encrypted: bool | None = None
    signature_valid: bool | None = None
    propagation_state: str | None = None


class RoutingMetadata(msgspec.Struct, frozen=True):
    """Metadata produced by the routing subsystem.

    Attributes
    ----------
    matched_routes:
        List of route identifiers that matched this event.
    fanout_group:
        Name of the fanout group, if the event was broadcast.
    """

    matched_routes: tuple[str, ...] = ()
    fanout_group: str | None = None


class RadioMetadata(msgspec.Struct, frozen=True):
    """RF-layer metadata for radio-transport events (e.g. Meshtastic).

    Attributes
    ----------
    snr:
        Signal-to-noise ratio in dB.
    rssi:
        Received signal strength indicator in dBm.
    channel_index:
        Radio channel index used for transmission.
    frequency:
        Operating frequency in MHz.
    """

    snr: float | None = None
    rssi: float | None = None
    channel_index: int | None = None
    frequency: float | None = None


class TelemetryMetadata(msgspec.Struct, frozen=True):
    """Arbitrary metrics from telemetry-producing events.

    Attributes
    ----------
    metrics:
        Flat dictionary of metric names to numeric or string values.
    """

    metrics: dict[str, float | int | str | bool] = msgspec.field(default_factory=dict)


class NativeMetadata(msgspec.Struct, frozen=True):
    """Opaque adapter-specific data that does not map to standard fields.

    Attributes
    ----------
    data:
        Arbitrary key-value data produced by the source adapter.
    """

    data: dict[str, object] = msgspec.field(default_factory=dict)


# ---------------------------------------------------------------------------
# Composite metadata
# ---------------------------------------------------------------------------


class EventMetadata(msgspec.Struct, frozen=True):
    """Top-level metadata container carried by every canonical event.

    Each namespace is optional – an event originating from a radio
    transport may populate :attr:`radio` while a message bridge event
    populates :attr:`native` instead.

    Attributes
    ----------
    transport:
        Transport-layer metadata (protocol, gateway, encryption).
    routing:
        Routing-layer metadata (matched routes, fanout).
    radio:
        RF-layer metadata (SNR, RSSI, frequency).
    telemetry:
        Telemetry metrics namespace.
    native:
        Opaque adapter-specific data.
    custom:
        Free-form dictionary for extensions and plugins.
    """

    transport: TransportMetadata | None = None
    routing: RoutingMetadata | None = None
    radio: RadioMetadata | None = None
    telemetry: TelemetryMetadata | None = None
    native: NativeMetadata | None = None
    custom: dict[str, object] = msgspec.field(default_factory=dict)
