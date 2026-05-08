"""Core event model package for the meshnet framework.

This package defines the foundational types that every other subsystem
depends on.  All public symbols are re-exported here for convenient
imports::

    from meshnet_framework.core.events import CanonicalEvent, EventKind

Re-exported symbols
-------------------
* From :mod:`~meshnet_framework.core.events.canonical`:
  ``CanonicalEvent``, ``EventRelation``, ``NativeRef``,
  ``NativeMessageRef``, ``DeliveryReceipt``, ``EventRecordKind``.
* From :mod:`~meshnet_framework.core.events.kinds`:
  ``EventKind``, ``KNOWN_KINDS``, ``is_registered``.
* From :mod:`~meshnet_framework.core.events.metadata`:
  ``EventMetadata``, ``TransportMetadata``, ``RoutingMetadata``,
  ``RadioMetadata``, ``TelemetryMetadata``, ``NativeMetadata``,
  ``MetadataEmbeddingMode``, ``PrivacyMode``.
* From :mod:`~meshnet_framework.core.events.schema`:
  ``SchemaRegistry``, ``SchemaVersion``, ``schema_version_from_event``.
"""

from meshnet_framework.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRecordKind,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from meshnet_framework.core.events.kinds import (
    KNOWN_KINDS,
    EventKind,
    is_registered,
)
from meshnet_framework.core.events.metadata import (
    EventMetadata,
    MetadataEmbeddingMode,
    NativeMetadata,
    PrivacyMode,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)
from meshnet_framework.core.events.schema import (
    SchemaRegistry,
    SchemaVersion,
    schema_version_from_event,
)

__all__ = [
    # canonical
    "CanonicalEvent",
    "DeliveryReceipt",
    "EventRecordKind",
    "EventRelation",
    "NativeMessageRef",
    "NativeRef",
    # kinds
    "EventKind",
    "KNOWN_KINDS",
    "is_registered",
    # metadata
    "EventMetadata",
    "MetadataEmbeddingMode",
    "NativeMetadata",
    "PrivacyMode",
    "RadioMetadata",
    "RoutingMetadata",
    "TelemetryMetadata",
    "TransportMetadata",
    # schema
    "SchemaRegistry",
    "SchemaVersion",
    "schema_version_from_event",
]
