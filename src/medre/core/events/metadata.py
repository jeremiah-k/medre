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
from msgspec.structs import force_setattr


# ---------------------------------------------------------------------------
# Internal immutable dict helper
# ---------------------------------------------------------------------------


class _FrozenDict(dict):
    """Dict subclass that prevents all mutation after construction.

    Used internally to provide deep immutability for dict fields in
    frozen msgspec structs while maintaining ``dict`` type compatibility
    for msgspec serialisation (``isinstance(_FrozenDict(), dict)`` is
    ``True``, so the encoder/decoder handles it transparently).
    """

    def __init__(self, *args, **kwargs):
        data = dict(*args, **kwargs)
        super().__init__(
            (key, self._freeze_value(value)) for key, value in data.items()
        )

    @classmethod
    def _freeze_value(cls, value):
        if isinstance(value, _FrozenDict):
            return value
        if isinstance(value, dict):
            return cls(value)
        if isinstance(value, list | tuple):
            return tuple(cls._freeze_value(item) for item in value)
        return value

    def __setitem__(self, key, value):
        raise TypeError("immutable mapping does not support item assignment")

    def __delitem__(self, key):
        raise TypeError("immutable mapping does not support item deletion")

    def clear(self):
        raise TypeError("immutable mapping does not support clear()")

    def pop(self, *args):
        raise TypeError("immutable mapping does not support pop()")

    def popitem(self):
        raise TypeError("immutable mapping does not support popitem()")

    def setdefault(self, key, default=None):
        raise TypeError("immutable mapping does not support setdefault()")

    def update(self, *args, **kwargs):
        raise TypeError("immutable mapping does not support update()")

    def __ior__(self, other):
        raise TypeError("immutable mapping does not support item assignment")


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
    route_trace:
        Ordered tuple of route IDs recorded per event delivery.
        Populated by the pipeline after route matching as attribution
        metadata.  Defaults to an empty tuple.
    """

    matched_routes: tuple[str, ...] = ()
    fanout_group: str | None = None
    route_trace: tuple[str, ...] = ()


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

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, _FrozenDict):
            force_setattr(self, "metrics", _FrozenDict(self.metrics))


class NativeMetadata(msgspec.Struct, frozen=True):
    """Opaque adapter-specific data that does not map to standard fields.

    Attributes
    ----------
    data:
        Arbitrary key-value data produced by the source adapter.
    """

    data: dict[str, object] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.data, _FrozenDict):
            force_setattr(self, "data", _FrozenDict(self.data))


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

    def __post_init__(self) -> None:
        if not isinstance(self.custom, _FrozenDict):
            force_setattr(self, "custom", _FrozenDict(self.custom))
