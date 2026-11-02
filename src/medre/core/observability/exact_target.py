"""Typed exact-target selection contract and read-only delivery preview.

This module is the closed contract for selecting exactly one persisted
delivery by its full ``(event_id, delivery_plan_id, target_adapter,
target_channel)`` identity and observing its current state as a typed,
schema-facing preview. It is a read-only observation, never a replay
execution grant and never a cancellation permit: every write path MUST
revalidate the exact identity, generation and ownership under storage
authority in its own transaction.

Selection and failure semantics:

* the selection key requires an explicit channel decision — a nonempty
  channel string, or ``None`` for the deliberate no-channel identity.
  The empty string is rejected at construction because storage and
  authority resolution normalize it to the no-channel identity, so
  passing it would silently widen the caller's intent;
* ``not_found`` is reserved for an event absent from canonical storage.
  Authorized event reads collapse absent and denied into one token, so
  existence is checked through metadata before any content read;
* the selection is fail-closed: an identity with no persisted outbox
  row, more than one currently-live outbox generation, or a frozen
  dispatch snapshot that cannot be interpreted classifies as
  ``ambiguous`` rather than guessing a winner;
* generations are reported raw. ``attempt_number`` is the persisted
  finalized counter (including zero for never-dispatched work) and
  ``active_attempt`` is the durable in-flight reservation; no field
  normalizes them into an effective generation, because write paths
  consume these fields and MUST NOT be fed display-normalized values;
* malformed selection input (incomplete identity, empty-string channel,
  non-integer generation) raises ``ValueError`` before storage is
  touched. Storage errors propagate with their own authority; only
  uninterpretable *persisted state* becomes ``ambiguous``.

The preview returns metadata only and never exposes stored message text.
For categories that need source content, the replay read internally projects
content after authorization. The preview issues no writes of its own; that
read creates no hold or claim and may durably record an expired mandatory
dependency under the content authority's short write transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, get_args

from medre.core.delivery_authority import (
    DeliveryIdentity,
    delivery_identity,
    select_current_outbox,
)
from medre.core.engine.pipeline.delivery_state import TERMINAL_OUTBOX_STATUSES
from medre.core.events import CanonicalEvent
from medre.core.observability.classification import failure_category
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshot,
    OutboxSnapshotIntegrityError,
    OutboxSnapshotReadError,
)
from medre.core.storage.read_contracts import AuthorizedEventRead

__all__ = [
    "EXACT_TARGET_CATEGORIES",
    "EXACT_TARGET_PREVIEW_SCOPE",
    "ExactTargetCategory",
    "ExactTargetPreview",
    "ExactTargetSelection",
    "ExactTargetStorageReader",
    "classify_outbox_status",
    "preview_exact_target",
]

#: Fixed preview scope, matching the untyped exact-recovery preview. The
#: preview observes the current persisted generation; it is not a replay
#: or cancellation grant.
EXACT_TARGET_PREVIEW_SCOPE: str = "read_only_current_generation"

#: Closed category vocabulary for exact-target observations. The nine
#: delivery-state tokens reuse :data:`RecoveryDisposition` so one authority
#: classifies delivery state; the five remaining tokens classify
#: exact-target selection outcomes. The vocabulary is frozen across this
#: ``Literal``, the JSON Schema enum, and the specification table.
ExactTargetCategory = Literal[
    "not_found",
    "ambiguous",
    "stale",
    "pending",
    "in_progress",
    "awaiting_feedback",
    "retry_scheduled",
    "completed",
    "cancelled",
    "inspect_native",
    "preview_replay",
    "needs_operator_action",
    "content_unavailable",
    "route_or_plan_changed",
]

EXACT_TARGET_CATEGORIES: frozenset[str] = frozenset(get_args(ExactTargetCategory))


@dataclass(frozen=True, slots=True)
class ExactTargetSelection:
    """Closed selection key wrapping one complete :class:`DeliveryIdentity`.

    Constructed from the four identity components so the channel decision
    is explicit before authority normalization applies. ``None`` is the
    deliberate no-channel identity; the empty string raises because
    persistence would silently fold it into ``None`` and widen the
    selection beyond the caller's stated intent.
    """

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    identity: DeliveryIdentity = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.target_channel == "":
            raise ValueError(
                "target_channel must be a nonempty channel string, or None for"
                " the deliberate no-channel identity; the empty string is not"
                " a channel decision"
            )
        identity = DeliveryIdentity(
            self.event_id,
            self.delivery_plan_id,
            self.target_adapter,
            self.target_channel,
        )
        if not identity.complete:
            raise ValueError(
                "Exact-target selection requires event, plan and adapter identity"
            )
        object.__setattr__(self, "identity", identity)


def classify_outbox_status(
    status: str | None,
    *,
    active_attempt: int | None = None,
    failure_kind: str | None = None,
) -> ExactTargetCategory:
    """Map one persisted outbox status observation to its category.

    Pure function; table-driven tests pin every row. Terminal persisted
    outcomes are authoritative and classify regardless of a residual
    reservation, which terminal settlement clears atomically. Among
    non-terminal states a live ``active_attempt`` reservation means a
    dispatch owner exists, so the row classifies as ``in_progress``
    exactly as the cancellation authority refuses it. A ``failed`` or
    ``dead_lettered`` row classifies by its persisted ``failure_kind``:
    a retryable kind previews a replay, every other value (including an
    absent kind) requires operator action. Any unrecognized status —
    including ``None`` — fails closed to ``ambiguous``.
    """
    if status == "sent":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status == "abandoned":
        return "inspect_native"
    if status in {"failed", "dead_lettered"}:
        if failure_category(failure_kind) == "retryable":
            return "preview_replay"
        return "needs_operator_action"
    if status == "in_progress" or active_attempt is not None:
        return "in_progress"
    if status == "pending":
        return "pending"
    if status == "queued":
        return "awaiting_feedback"
    if status == "retry_wait":
        return "retry_scheduled"
    return "ambiguous"


@dataclass(frozen=True, slots=True)
class ExactTargetPreview:
    """Typed read-only observation of one exact delivery target.

    Every field describes persisted state at observation time; none of
    them authorizes a mutation. ``attempt_number`` and ``active_attempt``
    are the raw persisted counters. ``observed_generation`` mirrors the
    finalized counter so callers never reconstruct an effective value.
    ``cancel_eligible`` mirrors the exact-cancellation authority's
    eligibility predicate (pending/retry_wait status without a
    reservation or claim) and is an observation, not a permit. The
    plan-binding fields are nullable: rows created before frozen
    dispatch snapshots keep their inline metadata and carry no snapshot
    binding, and terminal rows have their snapshot retired at
    settlement.
    """

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None
    category: ExactTargetCategory
    observed_fingerprint: str
    outbox_id: str | None = None
    attempt_number: int | None = None
    active_attempt: int | None = None
    status: str | None = None
    updated_at: str | None = None
    next_attempt_at: str | None = None
    payload_hash: str | None = None
    snapshot_present: bool = False
    snapshot_kind: str | None = None
    excluded_sibling_count: int = 0
    cancel_eligible: bool = False

    def __post_init__(self) -> None:
        if self.category not in EXACT_TARGET_CATEGORIES:
            raise ValueError(f"Unknown exact-target category: {self.category!r}")
        if self.excluded_sibling_count < 0:
            raise ValueError("excluded_sibling_count must not be negative")
        if len(self.observed_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.observed_fingerprint
        ):
            raise ValueError("observed_fingerprint must be 64 lowercase hex characters")

    @property
    def observed_generation(self) -> int | None:
        """The persisted finalized attempt counter, never display-normalized."""
        return self.attempt_number

    @property
    def execution_authorized(self) -> bool:
        """Always ``False``: an observation never authorizes execution."""
        return False

    @property
    def scope(self) -> str:
        """Fixed read-only scope shared with the untyped exact preview."""
        return EXACT_TARGET_PREVIEW_SCOPE

    def to_dict(self) -> dict[str, Any]:
        """Serialize the schema-facing preview with JSON-safe scalars only."""
        return {
            "scope": self.scope,
            "execution_authorized": self.execution_authorized,
            "event_id": self.event_id,
            "delivery_plan_id": self.delivery_plan_id,
            "target_adapter": self.target_adapter,
            "target_channel": self.target_channel,
            "outbox_id": self.outbox_id,
            "attempt_number": self.attempt_number,
            "active_attempt": self.active_attempt,
            "observed_generation": self.observed_generation,
            "status": self.status,
            "updated_at": self.updated_at,
            "next_attempt_at": self.next_attempt_at,
            "payload_hash": self.payload_hash,
            "snapshot_present": self.snapshot_present,
            "snapshot_kind": self.snapshot_kind,
            "category": self.category,
            "excluded_sibling_count": self.excluded_sibling_count,
            "cancel_eligible": self.cancel_eligible,
            "observed_fingerprint": self.observed_fingerprint,
        }


class ExactTargetStorageReader(Protocol):
    """Narrow read surface consumed by :func:`preview_exact_target`.

    ``SQLiteStorage`` satisfies this protocol structurally; the preview
    performs no other storage access.
    """

    async def get_metadata(self, event_id: str) -> CanonicalEvent | None: ...

    async def list_outbox_items_for_event(
        self, event_id: str, *, metadata_only: bool = False
    ) -> list[DeliveryOutboxItem]: ...

    async def read_event_for_replay(
        self, event_id: str, *, now: datetime | None = None
    ) -> AuthorizedEventRead: ...

    async def get_outbox_dispatch_context(
        self,
        outbox_id: str,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
    ) -> OutboxDispatchSnapshot | None: ...


def _observed_statement(
    identity: DeliveryIdentity, item: DeliveryOutboxItem | None
) -> dict[str, Any]:
    """Build the canonical metadata statement the fingerprint describes."""
    statement: dict[str, Any] = {
        "event_id": identity.event_id,
        "delivery_plan_id": identity.delivery_plan_id,
        "target_adapter": identity.target_adapter,
        "target_channel": identity.target_channel,
        "outbox_id": None,
        "attempt_number": None,
        "active_attempt": None,
        "status": None,
        "updated_at": None,
        "next_attempt_at": None,
        "payload_hash": None,
    }
    if item is not None:
        statement.update(
            {
                "outbox_id": item.outbox_id,
                "attempt_number": int(item.attempt_number),
                "active_attempt": item.active_attempt,
                "status": item.status,
                "updated_at": item.updated_at,
                "next_attempt_at": item.next_attempt_at,
                "payload_hash": item.payload_hash,
            }
        )
    return statement


def _fingerprint(statement: dict[str, Any]) -> str:
    """Hash one observation statement into a stable hex fingerprint."""
    return hashlib.sha256(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cancel_eligible(item: DeliveryOutboxItem) -> bool:
    """Mirror the exact-cancellation eligibility predicate.

    Matches the audit CHECK constraining observed status to
    ``pending``/``retry_wait`` and the CLI preview's refusal of rows
    with a reservation or claim ownership. Eligibility is an
    observation; the write transaction revalidates everything.
    """
    return (
        item.status in {"pending", "retry_wait"}
        and item.active_attempt is None
        and item.worker_id is None
        and item.locked_at is None
        and item.lease_until is None
    )


def _staged_binding_contradiction(
    snapshot: OutboxDispatchSnapshot, item: DeliveryOutboxItem
) -> bool | None:
    """Compare a frozen staged plan against the row's persisted binding.

    Return ``True`` when the frozen document names a different plan,
    event, route, or target than the outbox row persisted, ``False``
    when they agree, and ``None`` when the document cannot be
    interpreted (the caller fails closed). Reduced ``direct`` documents
    carry no plan or route identity fields: their binding is the row's
    own identity columns, which the exact selection already matched.
    """
    if snapshot.kind != "staged":
        return False
    plan = snapshot.context.get("plan")
    route = snapshot.context.get("route")
    target = plan.get("target") if isinstance(plan, dict) else None
    if (
        not isinstance(plan, dict)
        or not isinstance(route, dict)
        or not isinstance(target, dict)
    ):
        return None
    return (
        plan.get("plan_id") != item.delivery_plan_id
        or plan.get("event_id") != item.event_id
        or (plan.get("route_id") or "") != (item.route_id or "")
        or route.get("id") != item.route_id
        or target.get("adapter") != item.target_adapter
        or (target.get("channel") or None) != (item.target_channel or None)
    )


def _unmatched_preview(
    identity: DeliveryIdentity, category: ExactTargetCategory
) -> ExactTargetPreview:
    """Build the preview for an identity with no single selected row."""
    return ExactTargetPreview(
        event_id=identity.event_id,
        delivery_plan_id=identity.delivery_plan_id,
        target_adapter=identity.target_adapter,
        target_channel=identity.target_channel,
        category=category,
        observed_fingerprint=_fingerprint(_observed_statement(identity, None)),
    )


def _validate_expected_observation(
    expected_fingerprint: str | None, expected_generation: int | None
) -> None:
    """Reject malformed caller expectations before storage is touched."""
    if expected_fingerprint is not None and not expected_fingerprint:
        raise ValueError("expected_fingerprint must be a nonempty string")
    if expected_generation is not None and (
        type(expected_generation) is not int or expected_generation < 0
    ):
        raise ValueError(
            "expected_generation must be the persisted finalized attempt"
            " counter, including zero for never-dispatched work"
        )


async def preview_exact_target(
    storage: ExactTargetStorageReader,
    selection: ExactTargetSelection,
    *,
    expected_fingerprint: str | None = None,
    expected_generation: int | None = None,
) -> ExactTargetPreview:
    """Observe one exact delivery target without mutating storage.

    An absent event is ``not_found`` and an unselectable identity is
    ``ambiguous``. For statuses that require source content to consider
    recovery, unavailable content takes precedence over stale expectations
    and plan binding. For other statuses, persisted work state remains
    visible even after source collection. Stale expectations then precede
    plan-binding contradictions, which precede status classification.

    The expected generation is the persisted finalized attempt counter.
    It is superseded when it differs from the row's raw
    ``attempt_number`` or when the row now carries an
    ``active_attempt`` reservation, mirroring the exact-cancellation
    transaction's revalidation. The expected fingerprint must be a
    fingerprint this function previously reported.

    Sibling rows for the same event that the exact selection did not
    select are counted in ``excluded_sibling_count``; it is zero when
    no single row was selected. Malformed selection input raises
    ``ValueError``. A failed snapshot read propagates its storage
    error; a present but uninterpretable snapshot fails closed to
    ``ambiguous``. Storage errors from the metadata, outbox, and
    content reads propagate unchanged. The preview never returns
    stored message text or issues a lifecycle/content mutation of its own.
    A replay authorization read takes a short write transaction and may
    record a durable mandatory-dependency expiry observation.
    """
    _validate_expected_observation(expected_fingerprint, expected_generation)
    identity = selection.identity
    event = await storage.get_metadata(identity.event_id)
    if event is None:
        return _unmatched_preview(identity, "not_found")

    rows = await storage.list_outbox_items_for_event(
        identity.event_id, metadata_only=True
    )
    matches = [row for row in rows if delivery_identity(row) == identity]
    live = [row for row in matches if row.status not in TERMINAL_OUTBOX_STATUSES]
    if not matches or len(live) > 1:
        return _unmatched_preview(identity, "ambiguous")
    item = select_current_outbox(matches)
    if item is None:  # matches is non-empty; retained as a fail-closed guard
        return _unmatched_preview(identity, "ambiguous")

    binding: bool | None
    try:
        snapshot = await storage.get_outbox_dispatch_context(
            item.outbox_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
        )
    except OutboxSnapshotReadError:
        raise
    except OutboxSnapshotIntegrityError:
        # Only an observed but uninterpretable persisted document is ambiguous.
        # Backend errors other than snapshot integrity failures propagate.
        snapshot_present = True
        snapshot_kind = None
        binding = None
    else:
        snapshot_present = snapshot is not None
        snapshot_kind = snapshot.kind if snapshot is not None else None
        binding = (
            _staged_binding_contradiction(snapshot, item)
            if snapshot is not None
            else False
        )

    fingerprint = _fingerprint(_observed_statement(identity, item))
    status_category = classify_outbox_status(
        item.status,
        active_attempt=item.active_attempt,
        failure_kind=item.failure_kind,
    )
    # Completed/uncertain/actively owned outcomes require observation, not
    # source bytes. Reading replay content here would mask durable native
    # evidence after collection and could record an unrelated expiry.
    needs_content = status_category in {
        "pending",
        "retry_scheduled",
        "preview_replay",
        "needs_operator_action",
    }
    read = (
        await storage.read_event_for_replay(identity.event_id)
        if needs_content
        else None
    )
    if read is not None and read.status != "retained":
        category: ExactTargetCategory = "content_unavailable"
    elif (expected_fingerprint is not None and expected_fingerprint != fingerprint) or (
        expected_generation is not None
        and (
            item.attempt_number != expected_generation
            or item.active_attempt is not None
        )
    ):
        category = "stale"
    elif binding is True:
        category = "route_or_plan_changed"
    elif binding is None:
        category = "ambiguous"
    else:
        category = status_category

    return ExactTargetPreview(
        event_id=identity.event_id,
        delivery_plan_id=identity.delivery_plan_id,
        target_adapter=identity.target_adapter,
        target_channel=identity.target_channel,
        category=category,
        outbox_id=item.outbox_id,
        attempt_number=int(item.attempt_number),
        active_attempt=item.active_attempt,
        status=item.status,
        updated_at=item.updated_at,
        next_attempt_at=item.next_attempt_at,
        payload_hash=item.payload_hash,
        snapshot_present=snapshot_present,
        snapshot_kind=snapshot_kind,
        excluded_sibling_count=len(rows) - 1,
        cancel_eligible=_cancel_eligible(item),
        observed_fingerprint=fingerprint,
    )
