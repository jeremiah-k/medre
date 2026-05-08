"""Core event model package for the medre.

This package defines the foundational types that every other subsystem
depends on.  All public symbols are re-exported here for convenient
imports::

    from medre.core.events import CanonicalEvent, EventKind

Re-exported symbols
-------------------
* From :mod:`~medre.core.events.canonical`:
  ``CanonicalEvent``, ``EventRelation``, ``NativeRef``,
  ``NativeMessageRef``, ``DeliveryReceipt``, ``EventRecordKind``.
* From :mod:`~medre.core.events.kinds`:
  ``EventKind``, ``KNOWN_KINDS``, ``is_registered``.
* From :mod:`~medre.core.events.metadata`:
  ``EventMetadata``, ``TransportMetadata``, ``RoutingMetadata``,
  ``RadioMetadata``, ``TelemetryMetadata``, ``NativeMetadata``,
  ``MetadataEmbeddingMode``, ``PrivacyMode``.
* From :mod:`~medre.core.events.schema`:
  ``SchemaRegistry``, ``SchemaVersion``, ``schema_version_from_event``,
  ``CURRENT_SCHEMA_VERSION``, ``VALID_RELATION_TYPES``,
  ``MIGRATION_REGISTRY``.
"""

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRecordKind,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.kinds import (
    KNOWN_KINDS,
    EventKind,
    is_registered,
)
from medre.core.events.metadata import (
    EventMetadata,
    MetadataEmbeddingMode,
    NativeMetadata,
    PrivacyMode,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)
from medre.core.events.schema import (
    CURRENT_SCHEMA_VERSION,
    MIGRATION_REGISTRY,
    VALID_RELATION_TYPES,
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
    "CURRENT_SCHEMA_VERSION",
    "MIGRATION_REGISTRY",
    "VALID_RELATION_TYPES",
    "SchemaRegistry",
    "SchemaVersion",
    "schema_version_from_event",
]
