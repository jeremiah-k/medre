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
from typing import Any, AsyncGenerator, Protocol, runtime_checkable

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
# DeliveryOutboxItem
# ---------------------------------------------------------------------------


@dataclass
class DeliveryOutboxItem:
    """A single item in the local durable delivery outbox.

    Each item represents one delivery attempt for one target.  The outbox
    is operational work state — distinct from the evidence/audit log
    (:class:`DeliveryReceipt`).  Items are created after route/policy/loop/
    capacity acceptance and updated on each delivery attempt.

    Outbox statuses are:
    * ``pending`` — work exists but has not started.
    * ``in_progress`` — claimed by a worker for processing.
    * ``queued`` — handed to adapter-local queue (e.g. Meshtastic).
    * ``sent`` — local SDK/client send returned success (terminal).
    * ``retry_wait`` — transient failure, awaiting next attempt.
    * ``dead_lettered`` — retries exhausted or terminal failure (terminal).
    * ``cancelled`` — operator or shutdown cancelled (terminal).
    * ``abandoned`` — drain timeout or ambiguous loss (terminal).

    Attributes
    ----------
    outbox_id:
        Unique identifier (UUID).
    event_id:
        Canonical event ID to deliver.
    route_id:
        Route that triggered this delivery.
    delivery_plan_id:
        Delivery plan identifier.
    target_adapter:
        Target adapter name.
    target_channel:
        Target channel, if applicable.
    target_address:
        Target address if used by delivery planning.
    attempt_number:
        1-indexed attempt counter.
    status:
        Current outbox status.
    failure_kind:
        Failure classification from the most recent attempt, if any.
    failure_kind_detail:
        More specific failure detail, if any.
    next_attempt_at:
        ISO-8601 timestamp for next scheduled attempt (``retry_wait`` only).
    created_at:
        ISO-8601 timestamp of creation.
    updated_at:
        ISO-8601 timestamp of last update.
    last_attempt_at:
        ISO-8601 timestamp of most recent delivery attempt.
    locked_at:
        ISO-8601 timestamp when the item was claimed/locked.
    lease_until:
        ISO-8601 timestamp when the current lease expires.
    worker_id:
        Identifier of the worker holding the lease.
    payload_hash:
        Hash of render inputs, for change detection after restart.
    receipt_id:
        Most recent delivery receipt ID for this attempt.
    parent_receipt_id:
        Previous receipt ID in retry lineage.
    error_summary:
        Sanitised, capped error string from the most recent attempt.
    metadata:
        JSON-safe dict for non-secret transport-neutral details.
    """

    outbox_id: str
    event_id: str
    route_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None = None
    target_address: str | None = None
    attempt_number: int = 1
    status: str = "pending"
    failure_kind: str | None = None
    failure_kind_detail: str | None = None
    next_attempt_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_attempt_at: str | None = None
    locked_at: str | None = None
    lease_until: str | None = None
    worker_id: str | None = None
    payload_hash: str | None = None
    receipt_id: str | None = None
    parent_receipt_id: str | None = None
    error_summary: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        """Return ``True`` if the status is a terminal (non-recoverable) state."""
        # Deferred import to avoid circular dependency at module load time
        # (backend -> delivery_state -> engine package init -> pipeline init
        # -> delivery_lifecycle -> backend).
        from medre.core.engine.pipeline.delivery_state import (
            is_terminal_outbox_status,
        )

        return is_terminal_outbox_status(self.status)

    @property
    def is_claimable(self) -> bool:
        """Return ``True`` if this item is in a *directly*-claimable status.

        This property reflects **direct** claimability only: items whose
        ``status`` is ``pending`` or ``retry_wait`` can be claimed
        immediately by any worker via :meth:`claim_due_outbox_items`.

        Items in ``in_progress`` or ``queued`` are **not** directly
        claimable, but *may* become reclaimable through storage-level
        queries that check for lease expiry (``in_progress`` whose
        ``lease_until`` has passed) or staleness (``queued`` whose
        ``updated_at`` is older than a grace threshold).  Those reclaim
        paths are entirely storage-query-dependent and are **not**
        reflected by this property.

        Returns
        -------
        bool
        """
        # Deferred import to avoid circular dependency (see is_terminal).
        from medre.core.engine.pipeline.delivery_state import (
            is_claimable_outbox_status,
        )

        return is_claimable_outbox_status(self.status)


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

    def query(self, filter: EventFilter) -> AsyncGenerator[CanonicalEvent, None]:
        """Yield events matching *filter*, ordered by timestamp ascending.

        Implementations use ``async def`` with ``yield`` (async generator).
        The protocol declares this as a regular ``def`` returning
        :class:`AsyncGenerator` because calling an async generator function
        returns an ``AsyncGenerator`` directly, not a ``Coroutine`` wrapping
        one.
        """
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
        target_channel: str | None = None,
    ) -> DeliveryReceipt | None:
        """Return the latest receipt for a delivery plan / adapter / channel triple.

        Parameters
        ----------
        delivery_plan_id:
            The delivery plan to look up.
        target_adapter:
            The target adapter to filter on.
        target_channel:
            Channel name to match.  When a named channel is passed, only
            receipts with that exact channel value are returned.  When
            ``None`` (default), only receipts with a NULL (no-channel)
            target are returned.  Passing ``None`` does **not** query
            across all channels.

        Returns
        -------
        DeliveryReceipt | None
            The latest-matching receipt, or ``None`` when no receipt exists
            for the given combination.
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

    # -- Outbox -------------------------------------------------------------

    async def create_outbox_item(self, item: DeliveryOutboxItem) -> DeliveryOutboxItem:
        """Create a new outbox item.

        Checks for an existing item with the same key tuple
        ``(delivery_plan_id, target_adapter, target_channel,
        attempt_number)`` before inserting.  The behaviour depends on
        the existing row's status (see ``sqlite.py`` for the canonical
        implementation):

        * **Reclaimable** (``pending`` or ``retry_wait``): the existing
          row is reclaimed — its ``status``, ``worker_id``,
          ``locked_at``, ``lease_until``, and ``updated_at`` are
          updated so the caller receives a properly-claimed row.
        * **Active** (``in_progress`` or ``queued``): returned
          unchanged — active work is never stolen.
        * **Terminal** (``sent``, ``dead_lettered``, ``cancelled``, or
          ``abandoned``): the existing row is deleted first and a new
          row is inserted so re-delivery can proceed without violating
          the UNIQUE constraint.
        """
        ...

    async def get_outbox_item(self, outbox_id: str) -> DeliveryOutboxItem | None:
        """Retrieve a single outbox item by its ID.

        Returns ``None`` when no item with *outbox_id* exists.
        """
        ...

    async def get_outbox_item_for_delivery(
        self,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        status: str | None = None,
    ) -> DeliveryOutboxItem | None:
        """Retrieve an outbox item by its delivery target key.

        Performs a targeted SELECT matching *event_id*,
        *delivery_plan_id*, *target_adapter*, *target_channel*
        (using ``IS`` for proper ``NULL`` handling) and optionally
        *status*.  Returns the first match or ``None``.

        This replaces the O(n) scan previously needed to locate an
        outbox item for a specific delivery target.
        """
        ...

    async def list_outbox_items(
        self,
        status_filter: list[str] | None = None,
        due_before: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DeliveryOutboxItem]:
        """List outbox items matching optional status and due filters.

        Ordered by ``next_attempt_at ASC, created_at ASC`` so that
        due items appear first.
        """
        ...

    async def claim_due_outbox_items(
        self,
        now: str,
        worker_id: str,
        lease_seconds: int = 30,
        limit: int = 20,
    ) -> list[DeliveryOutboxItem]:
        """Atomically claim due outbox items for processing.

        Uses a transaction to SELECT candidates and UPDATE in one step.
        Claims items matching **any** of these status conditions:

        - ``status IN ('pending', 'retry_wait')`` — directly
          claimable;
        - ``status = 'in_progress' AND lease_until <= now`` — expired
          leases (worker crashed or stalled);
        - ``status = 'queued' AND updated_at <= now - GRACE`` — stale
          queued items past the grace threshold defined by
          :data:`~medre.core.storage.sqlite.STALE_QUEUED_GRACE_SECONDS`
          (default 300 s).

        Additional guards applied to every candidate:

        - ``(next_attempt_at IS NULL OR next_attempt_at <= now)``
        - ``(lease_until IS NULL OR lease_until <= now)``

        Each claimed item is set to ``status='in_progress'`` with
        ``locked_at=now``, ``lease_until=now+lease_seconds``,
        ``worker_id=worker_id``, and ``next_attempt_at=NULL``.
        Returns the claimed items.

        The operation is atomic: two concurrent calls with the same
        criteria receive disjoint sets of items.
        """
        ...

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``sent`` (terminal).

        Only transitions from ``in_progress`` or ``queued``.  No-op if
        already terminal.
        """
        ...

    async def mark_outbox_queued(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``queued`` (adapter-local queue acceptance).

        Only transitions from ``in_progress``.  No-op if already terminal.
        """
        ...

    async def mark_outbox_retry_wait(
        self,
        outbox_id: str,
        next_attempt_at: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
        attempt_number: int | None = None,
    ) -> None:
        """Mark an outbox item as ``retry_wait`` (transient failure).

        Sets ``next_attempt_at`` for the next scheduled attempt.
        Only transitions from ``in_progress``.
        """
        ...

    async def mark_outbox_dead_lettered(
        self,
        outbox_id: str,
        receipt_id: str | None = None,
        failure_kind: str | None = None,
        failure_kind_detail: str | None = None,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``dead_lettered`` (terminal failure).

        Only transitions from ``in_progress`` or ``retry_wait``.
        No-op if already terminal.
        """
        ...

    async def mark_outbox_cancelled(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``cancelled`` (terminal).

        May be called from any non-terminal status.  No-op if already
        terminal.
        """
        ...

    async def mark_outbox_abandoned(
        self,
        outbox_id: str,
        error_summary: str | None = None,
    ) -> None:
        """Mark an outbox item as ``abandoned`` (terminal).

        Used for in-flight items lost at drain timeout.  No-op if already
        terminal.
        """
        ...

    async def renew_outbox_lease(
        self,
        outbox_id: str,
        worker_id: str,
        lease_until: str,
    ) -> bool:
        """Renew the lease on an in_progress outbox item.

        Returns True if the lease was renewed, False if the item is no
        longer owned by this worker or is not in_progress.
        """
        ...

    async def release_outbox_claim(
        self,
        outbox_id: str,
        worker_id: str,
        *,
        release_status: str = "pending",
    ) -> None:
        """Release a claim on an outbox item, restoring its prior status.

        Clears ``locked_at``, ``lease_until``, and ``worker_id`` and sets
        ``status`` to *release_status* (default ``"pending"``).
        Only succeeds when the current ``worker_id`` matches.
        """
        ...

    async def count_outbox_by_status(self) -> dict[str, int]:
        """Return counts of outbox items grouped by status.

        Returns a dict mapping status strings to counts, e.g.
        ``{"pending": 3, "retry_wait": 2, "sent": 5, ...}``.
        Includes all statuses present in the table.
        """
        ...

    # -- Lifecycle ----------------------------------------------------------

    async def initialize(self) -> None:
        """Prepare the backend for use (open connections, create schema)."""
        ...

    async def close(self) -> None:
        """Release all resources held by the backend."""
        ...
