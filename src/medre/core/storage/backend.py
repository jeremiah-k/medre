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
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
)


# ---------------------------------------------------------------------------
# Shared defaults
# ---------------------------------------------------------------------------

#: Maximum number of events returned by a single query when the caller
#: does not specify an explicit limit.  Used by both :class:`EventFilter`
#: (low-level storage queries) and :class:`ReplayRequest` (high-level
#: replay operations).  Callers that need different paging behaviour
#: should pass an explicit ``limit`` value.
DEFAULT_QUERY_LIMIT: int = 1000


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


class DuplicateEventError(StorageError):
    """Raised when attempting to append a canonical event whose ``event_id``
    already exists in the store.

    Events are append-only; callers that need idempotent semantics should
    check with :meth:`~StorageBackend.get` before calling
    :meth:`~StorageBackend.append`, or catch this exception.
    """


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
        Maximum number of events to return.  Defaults to
        :data:`DEFAULT_QUERY_LIMIT` (``1000``).
    """

    event_kinds: list[str] | None = None
    source_adapters: list[str] | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    limit: int = DEFAULT_QUERY_LIMIT


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
        """Persist a canonical event together with its inline relations.

        Raises :class:`DuplicateEventError` when *event.event_id* already
        exists in the store.
        """
        ...

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by its unique identifier.

        Returns ``None`` when no event with *event_id* exists.
        """
        ...

    async def query(self, filter: EventFilter) -> AsyncIterator[CanonicalEvent]:
        """Yield events matching *filter*, ordered by timestamp ascending."""
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

    async def list_native_refs_for_event(
        self,
        event_id: str,
    ) -> list[NativeMessageRef]:
        """Return all native message refs for a specific event.

        Native refs are ordered by ``created_at`` ascending.
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
        """Append a delivery receipt record.

        The receipt's ``source`` field indicates the origin (``"live"`` or
        ``"replay"``); ``replay_run_id`` is populated when
        ``source="replay"``.  The ``target_channel`` field records the
        channel the event was delivered to, enabling retry reconstruction.
        """
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

    async def list_receipts_for_plan(
        self,
        delivery_plan_id: str,
        target_adapter: str,
    ) -> list[DeliveryReceipt]:
        """Return all receipts for a delivery plan / adapter in attempt order.

        Receipts are ordered by ``attempt_number`` ascending so callers
        can walk the full receipt lineage.
        """
        ...

    async def list_receipts_by_replay_run(
        self,
        run_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all receipts produced by a specific replay run.

        Receipts are ordered by ``sequence`` ascending.  Only receipts
        with ``source='replay'`` and the given ``replay_run_id`` are
        returned.  Returns an empty list when no receipts match.
        """
        ...

    async def list_receipts_for_event(
        self,
        event_id: str,
    ) -> list[DeliveryReceipt]:
        """Return all delivery receipts for a specific event.

        Receipts are ordered by ``sequence`` ascending, which reflects
        the chronological append order across all delivery plans and
        adapters for this event.
        """
        ...

    # -- Counts -------------------------------------------------------------

    async def count_events(self) -> int:
        """Return the total number of persisted canonical events."""
        ...

    async def count_receipts(self) -> int:
        """Return the total number of delivery receipt rows."""
        ...

    async def count_native_refs(self) -> int:
        """Return the total number of native message ref records."""
        ...

    async def count_receipts_by_source(self, source: str) -> int:
        """Return the number of delivery receipts matching *source*."""
        ...

    async def count_replay_runs(self) -> int:
        """Return the number of distinct ``replay_run_id`` values."""
        ...

    # -- Retry --------------------------------------------------------------

    async def list_due_retry_receipts(
        self, now: datetime, limit: int = 50, max_attempts: int = 3
    ) -> list[Any]:
        """Return transient-failure receipts whose next_retry_at <= now.

        Ordered by next_retry_at ASC, sequence ASC, limited to *limit*.
        Excludes receipts that have reached *max_attempts* or are dead_lettered.
        """
        ...

    async def count_pending_retry(self, now: datetime, max_attempts: int = 3) -> int:
        """Count transient-failure receipts due for retry."""
        ...

    async def update_retry_due(
        self, receipt_id: str, next_retry_at: datetime,
    ) -> None:
        """Update next_retry_at on a receipt (for capacity rejection backoff).

        This is the only mutation allowed on existing receipt rows — all
        other receipt updates are append-only.
        """
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema)."""
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...
