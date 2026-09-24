"""Shared test helper for constructing DeliveryOutboxItem rows.

Centralises the ``_make_outbox_item`` factory that was previously duplicated
across the split storage outbox test modules.  Defaults match the original
in-test factories byte-for-byte.

This helper is a **pure factory** — it does NOT validate ``status``.  The
production ``create_outbox_item()`` enforces that only ``pending`` and
``in_progress`` are accepted as initial statuses.  Tests that need a row
in another status (queued, sent, retry_wait, dead_lettered, cancelled,
abandoned) may construct the item freely via this helper and then call the
appropriate ``mark_outbox_*`` transition method.

**When bypassing ``create_outbox_item()`` is acceptable:**

- Pure property / unit tests that check read-only predicates
  (``is_claimable``, ``is_terminal``, etc.) without exercising storage.

**When bypassing ``create_outbox_item()`` is NOT acceptable:**

- Behaviour tests that verify the storage lifecycle — reclaim,
  transition, or finalization flows.  These must go through
  ``create_outbox_item()`` so the production validation gate is
  exercised end-to-end.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    NativeMessageRef,
)
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    StorageBackend,
    TerminalOutboxFinalization,
)

SENTINEL_EVENT_ID: str = "__outbox_default__"


def make_outbox_item(
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "fake_presentation",
    target_channel: str | None = "ch-0",
    attempt_number: int = 1,
    status: str = "pending",
    next_attempt_at: str | None = None,
    event_id: str = SENTINEL_EVENT_ID,
) -> DeliveryOutboxItem:
    """Build a minimal DeliveryOutboxItem for tests.

    No validation is performed here; ``create_outbox_item()`` enforces
    the production lifecycle policy.  See module docstring.
    """
    return DeliveryOutboxItem(
        outbox_id=f"obox-{uuid.uuid4()}",
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        attempt_number=attempt_number,
        status=status,
        next_attempt_at=next_attempt_at,
    )


async def admit_event(storage: StorageBackend, event_id: str) -> None:
    """Ensure a minimal canonical parent exists for direct FK-dependent writes."""
    if await storage.get(event_id) is not None:
        return
    await storage.append(
        CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(UTC),
            source_adapter="test-fixture",
            source_transport_id="t1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "foreign-key fixture parent"},
            metadata=EventMetadata(),
        )
    )


async def admit_default_event(storage: StorageBackend) -> None:
    """Append the sentinel canonical event used by ``make_outbox_item()``."""
    await admit_event(storage, SENTINEL_EVENT_ID)


async def create_outbox_item_with_parent(
    storage: StorageBackend, item: DeliveryOutboxItem
) -> DeliveryOutboxItem:
    """Admit the referenced event, then exercise production outbox creation."""
    await admit_event(storage, item.event_id)
    return await storage.create_outbox_item(item)


async def append_receipt_with_parent(
    storage: StorageBackend, receipt: DeliveryReceipt
) -> None:
    """Admit the referenced event, then append the receipt."""
    await admit_event(storage, receipt.event_id)
    await storage.append_receipt(receipt)


async def store_native_ref_with_parent(
    storage: StorageBackend, ref: NativeMessageRef
) -> None:
    """Admit the referenced event, then persist the native reference."""
    await admit_event(storage, ref.event_id)
    await storage.store_native_ref(ref)


def _same_outbox_identity(left: DeliveryOutboxItem, right: DeliveryOutboxItem) -> bool:
    """Return whether two rows belong to the same event-scoped delivery identity."""
    return (
        left.event_id == right.event_id
        and left.delivery_plan_id == right.delivery_plan_id
        and left.target_adapter == right.target_adapter
        and (left.target_channel or None) == (right.target_channel or None)
    )


def _effective_outbox_attempt(item: DeliveryOutboxItem) -> int:
    """Return the generation represented by an outbox row."""
    return (
        item.active_attempt if item.active_attempt is not None else item.attempt_number
    )


def allocate_new_outbox_generation(
    items: dict[str, DeliveryOutboxItem],
    candidate: DeliveryOutboxItem,
) -> DeliveryOutboxItem:
    """Allocate one fresh generation above all effective sibling attempts.

    This mirrors SQLite's atomic replay allocator for in-memory test stores.
    The caller is responsible for storing the returned row.
    """
    if candidate.active_attempt is not None:
        raise ValueError("allocate_new_generation requires active_attempt=None")

    max_attempt = 0
    for existing in items.values():
        if _same_outbox_identity(existing, candidate):
            max_attempt = max(max_attempt, _effective_outbox_attempt(existing))
    return replace(candidate, attempt_number=max_attempt + 1, active_attempt=None)


def find_existing_outbox_generation(
    items: dict[str, DeliveryOutboxItem],
    candidate: DeliveryOutboxItem,
) -> DeliveryOutboxItem | None:
    """Mirror SQLite create-time generation reuse for in-memory fakes.

    A finalized ``attempt_number`` or live ``active_attempt`` already represents
    that generation.  Returning the existing row prevents a replay create from
    manufacturing a sibling generation that a live retry has already reserved.

    Claimable rows (``pending`` / ``retry_wait``) are reclaimed exactly as
    SQLite's create transaction does: status, worker, and lease adopt the
    candidate's values and stale scheduling/reservation state clears.
    """
    for existing in items.values():
        if not _same_outbox_identity(existing, candidate):
            continue
        if candidate.attempt_number in {
            existing.attempt_number,
            existing.active_attempt,
        }:
            if existing.status in ("pending", "retry_wait"):
                object.__setattr__(existing, "status", candidate.status or "pending")
                object.__setattr__(existing, "worker_id", candidate.worker_id)
                object.__setattr__(existing, "locked_at", candidate.locked_at)
                object.__setattr__(existing, "lease_until", candidate.lease_until)
                object.__setattr__(existing, "next_attempt_at", None)
                object.__setattr__(existing, "active_attempt", None)
            return existing
    return None


def reserve_guarded_outbox_attempt(
    items: dict[str, DeliveryOutboxItem],
    outbox_id: str,
    worker_id: str,
    from_attempt: int,
) -> int | None:
    """Mirror SQLite's sibling-aware guarded attempt reservation.

    The next generation may be reserved only by the claimed row and only when
    no sibling row for the same event-scoped delivery identity already
    represents that generation or a newer one.  This keeps in-memory lifecycle
    fakes aligned with the production replay/retry collision fence.
    """
    item = items.get(outbox_id)
    if (
        item is None
        or item.status != "in_progress"
        or item.worker_id != worker_id
        or item.active_attempt is not None
        or item.attempt_number != from_attempt
    ):
        return None

    next_attempt = from_attempt + 1
    for sibling in items.values():
        if sibling.outbox_id == item.outbox_id:
            continue
        if not _same_outbox_identity(sibling, item):
            continue
        if _effective_outbox_attempt(sibling) >= next_attempt:
            return None

    object.__setattr__(item, "active_attempt", next_attempt)
    return next_attempt


def apply_guarded_outbox_transition(
    item: DeliveryOutboxItem,
    new_status: str,
    *,
    allowed_from: tuple[str, ...] | None = None,
    attempt_number: int | None = None,
    expected_worker_id: str | None = None,
    receipt_id: str | None = None,
    failure_kind: str | None = None,
    failure_kind_detail: str | None = None,
    error_summary: str | None = None,
    next_attempt_at: str | None = None,
) -> bool:
    """In-memory mirror of ``SQLiteStorage._update_outbox_status`` guards.

    Test fakes use this so their ``mark_outbox_*`` methods honour the same
    contract as the SQLite backend: terminal rows are immutable,
    ``allowed_from`` source statuses are enforced, ``expected_worker_id``
    fences the transition to the current claim owner, and explicit attempt
    commits follow the monotonic reservation fence — a live reservation
    must match exactly, while an unreserved row may only preserve or
    advance its finalized attempt number.  Transition-specific metadata
    (``receipt_id``, ``failure_kind``, ``failure_kind_detail``,
    ``error_summary``, ``next_attempt_at``) is applied after the guard
    commits, with the queued/sent failure-field clears winning as in SQL.
    Claim metadata (``worker_id``, ``locked_at``, ``lease_until``) is
    cleared on the transitions that release the claim in SQLite.  Returns
    ``True`` only when the transition committed.
    """
    from medre.core.engine.pipeline.delivery_state import (
        TERMINAL_OUTBOX_STATUSES,
    )

    if item.status in TERMINAL_OUTBOX_STATUSES:
        return False
    if allowed_from is not None and item.status not in allowed_from:
        return False
    if expected_worker_id is not None and item.worker_id != expected_worker_id:
        return False
    if attempt_number is not None and not (
        (item.active_attempt is None and item.attempt_number <= attempt_number)
        or item.active_attempt == attempt_number
    ):
        return False

    object.__setattr__(item, "status", new_status)
    if attempt_number is not None:
        object.__setattr__(item, "attempt_number", attempt_number)
        object.__setattr__(item, "active_attempt", None)
    elif new_status != "in_progress":
        object.__setattr__(
            item,
            "attempt_number",
            (
                item.active_attempt
                if item.active_attempt is not None
                else item.attempt_number
            ),
        )
        object.__setattr__(item, "active_attempt", None)
    if receipt_id is not None:
        object.__setattr__(item, "receipt_id", receipt_id)
    if failure_kind is not None:
        object.__setattr__(item, "failure_kind", failure_kind)
    if failure_kind_detail is not None:
        object.__setattr__(item, "failure_kind_detail", failure_kind_detail)
    if error_summary is not None:
        object.__setattr__(item, "error_summary", error_summary)
    if next_attempt_at is not None:
        object.__setattr__(item, "next_attempt_at", next_attempt_at)
    if new_status in ("queued", "sent"):
        # The queued/sent clears win over any supplied failure metadata,
        # matching the duplicate-assignment resolution in the SQL UPDATE.
        object.__setattr__(item, "failure_kind", None)
        object.__setattr__(item, "failure_kind_detail", None)
        object.__setattr__(item, "error_summary", None)
    if new_status in TERMINAL_OUTBOX_STATUSES or new_status in ("queued", "retry_wait"):
        object.__setattr__(item, "locked_at", None)
        object.__setattr__(item, "lease_until", None)
        object.__setattr__(item, "worker_id", None)
    return True


def apply_guarded_outbox_terminal(
    item: DeliveryOutboxItem | None,
    receipts: list[DeliveryReceipt],
    command: TerminalOutboxFinalization,
) -> bool:
    """Mirror SQLite's guarded atomic terminal finalization for test fakes.

    Validation happens before the outbox guard, matching the concrete storage
    API. When the guard rejects, neither immutable receipts nor mutable outbox
    authority changes. A committed transition clears the same retry and claim
    metadata as SQLite in the terminal transaction.
    """
    receipt = command.lifecycle_receipt
    attempt_receipt = command.attempt_receipt
    identity = command.identity

    effective_attempt = (
        item.active_attempt
        if item is not None and item.active_attempt is not None
        else item.attempt_number if item is not None else None
    )
    if (
        item is None
        or item.outbox_id != command.outbox_id
        or item.event_id != identity.event_id
        or item.delivery_plan_id != identity.delivery_plan_id
        or item.target_adapter != identity.target_adapter
        or (item.target_channel or None) != identity.target_channel
        or item.status not in {"queued", "in_progress"}
        or effective_attempt != command.attempt_number
        or (
            command.expected_worker_id is not None
            and item.worker_id != command.expected_worker_id
        )
    ):
        return False

    if attempt_receipt is not None:
        receipts.append(attempt_receipt)
    receipts.append(receipt)
    object.__setattr__(item, "status", command.terminal_status)
    object.__setattr__(item, "attempt_number", command.attempt_number)
    object.__setattr__(item, "active_attempt", None)
    object.__setattr__(item, "receipt_id", receipt.receipt_id)
    object.__setattr__(item, "failure_kind", command.failure_kind)
    object.__setattr__(item, "failure_kind_detail", None)
    object.__setattr__(item, "error_summary", command.error_summary)
    object.__setattr__(item, "next_attempt_at", None)
    object.__setattr__(item, "worker_id", None)
    object.__setattr__(item, "locked_at", None)
    object.__setattr__(item, "lease_until", None)
    return True
