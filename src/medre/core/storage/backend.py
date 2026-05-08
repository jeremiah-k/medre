"""Storage backend protocol and supporting types.

This module defines the contract that every storage implementation must
satisfy, along with helper types used throughout the storage subsystem:

* :class:`StorageBackend` – runtime-checkable protocol.
* :class:`EventFilter` – criteria for event queries.
* :class:`StorageGuarantees` – behavioural guarantees descriptor.
* :class:`StorageError` hierarchy – structured exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Protocol, runtime_checkable

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Base exception for all storage-related errors."""


class EventNotFoundError(StorageError):
    """Raised when an event cannot be found by its identifier."""


class StorageInitializationError(StorageError):
    """Raised when the storage backend fails to initialise or is used
    before ``initialize()`` has been called."""


class SchemaValidationError(StorageError):
    """Raised when stored data does not conform to the expected schema."""


# ---------------------------------------------------------------------------
# Event filter
# ---------------------------------------------------------------------------


@dataclass
class EventFilter:
    """Criteria for querying events from the storage backend.

    All fields are optional; a value of ``None`` means *no restriction*
    for that dimension.

    Attributes
    ----------
    event_kinds:
        Restrict results to these event kind strings.
    source_adapters:
        Restrict results to events produced by these adapters.
    time_start:
        Earliest event timestamp (inclusive).
    time_end:
        Latest event timestamp (inclusive).
    limit:
        Maximum number of events to return.
    """

    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    limit: int = 100


# ---------------------------------------------------------------------------
# Guarantees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageGuarantees:
    """Behavioural guarantees advertised by a storage backend.

    Callers may inspect these at runtime to decide whether a particular
    backend satisfies their consistency requirements.

    Attributes
    ----------
    durable:
        Written data survives process restarts.
    ordered:
        Events can be read back in append order.
    transactional:
        Multiple writes can be grouped atomically.
    concurrent_reads:
        The backend supports concurrent read operations.
    concurrent_writes:
        The backend supports concurrent write operations.
    """

    durable: bool = True
    ordered: bool = False
    transactional: bool = True
    concurrent_reads: bool = True
    concurrent_writes: bool = False


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol defining the interface all storage backends must implement.

    Every method is async to allow implementations that perform I/O
    (network databases, remote APIs, …) without blocking the event loop.

    Contractual guarantees
    ----------------------
    Events are append-only.  Delivery receipts are append-only.
    Native refs are idempotent.  Relations queryable by event_id.
    Query results ordered by timestamp ascending.
    """

    # -- Event CRUD ---------------------------------------------------------

    async def append(self, event: CanonicalEvent) -> None:
        """Persist a canonical event together with its inline relations."""
        ...

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by its unique identifier.

        Returns ``None`` when no event with *event_id* exists.
        """
        ...

    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]:
        """Yield events matching *filter*, newest-first."""
        ...

    # -- Native ref correlation ---------------------------------------------

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        """Persist a native-to-canonical message mapping."""
        ...

    async def resolve_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> str | None:
        """Look up the canonical event ID for a native message reference.

        Returns ``None`` when no mapping exists for the given triple.
        """
        ...

    # -- Relations ----------------------------------------------------------

    async def store_relation(self, event_id: str, relation: EventRelation) -> None:
        """Persist a single relation for an existing event."""
        ...

    async def list_relations(self, event_id: str) -> list[EventRelation]:
        """Return all relations belonging to *event_id*."""
        ...

    # -- Receipts -----------------------------------------------------------

    async def append_receipt(self, receipt: DeliveryReceipt) -> None:
        """Append a delivery receipt record."""
        ...

    async def delivery_status(
        self,
        delivery_plan_id: str,
        target_adapter: str,
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter pair.

        Returns ``None`` when no receipt exists for the given combination.
        """
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema)."""
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...
