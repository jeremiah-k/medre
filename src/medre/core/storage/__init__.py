"""Storage subsystem for the medre.

Package-level imports
---------------------
* :class:`StorageBackend` – protocol that all backends implement.
* :class:`SQLiteStorage` – built-in SQLite-backed implementation.
* :class:`EventFilter` – filter criteria for event queries.
* :class:`StorageGuarantees` – behavioural guarantees descriptor.
* :class:`StorageError` – base exception for storage failures.
* :class:`DuplicateEventError` – duplicate event append error.
* :class:`EventNotFoundError` – event-not-found exception.
* :class:`StorageInitializationError` – initialisation failure.
* :class:`SchemaValidationError` – schema validation failure.
* From :mod:`~medre.core.storage.replay`:
  ``ReplayMode``, ``ReplayRequest``, ``ReplayResult``, ``ReplayEngine``,
  ``ReplayRouteAttribution``, ``ReplaySummary``, ``collect_replay_summary``.
"""

from medre.core.storage.backend import (
    DeliveryOutboxItem,
    DuplicateEventError,
    EventFilter,
    EventNotFoundError,
    SchemaValidationError,
    StorageBackend,
    StorageError,
    StorageGuarantees,
    StorageInitializationError,
)
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    ReplaySummary,
    collect_replay_summary,
)
from medre.core.storage.sqlite import SQLiteStorage

__all__ = [
    "DeliveryOutboxItem",
    "DuplicateEventError",
    "EventFilter",
    "EventNotFoundError",
    "ReplayEngine",
    "ReplayMode",
    "ReplayRequest",
    "ReplayResult",
    "ReplayRouteAttribution",
    "ReplaySummary",
    "SchemaValidationError",
    "SQLiteStorage",
    "StorageBackend",
    "StorageError",
    "StorageGuarantees",
    "StorageInitializationError",
    "collect_replay_summary",
]
