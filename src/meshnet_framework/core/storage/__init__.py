"""Storage subsystem for the meshnet framework.

Re-exported symbols
-------------------
* :class:`StorageBackend` – protocol that all backends implement.
* :class:`SQLiteStorage` – built-in SQLite-backed implementation.
* :class:`EventFilter` – filter criteria for event queries.
* :class:`StorageGuarantees` – behavioural guarantees descriptor.
* :class:`StorageError` – base exception for storage failures.
* :class:`EventNotFoundError` – event-not-found exception.
* :class:`StorageInitializationError` – initialisation failure.
* :class:`SchemaValidationError` – schema validation failure.
* From :mod:`~meshnet_framework.core.storage.replay`:
  ``ReplayMode``, ``ReplayRequest``, ``ReplayResult``, ``ReplayEngine``.
"""

from meshnet_framework.core.storage.backend import (
    EventFilter,
    EventNotFoundError,
    SchemaValidationError,
    StorageBackend,
    StorageError,
    StorageGuarantees,
    StorageInitializationError,
)
from meshnet_framework.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
)
from meshnet_framework.core.storage.sqlite import SQLiteStorage

__all__ = [
    "EventFilter",
    "EventNotFoundError",
    "ReplayEngine",
    "ReplayMode",
    "ReplayRequest",
    "ReplayResult",
    "SchemaValidationError",
    "SQLiteStorage",
    "StorageBackend",
    "StorageError",
    "StorageGuarantees",
    "StorageInitializationError",
]
