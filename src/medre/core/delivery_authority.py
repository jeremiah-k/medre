"""Shared current-delivery authority resolution.

The immutable receipt log records history, while a delivery outbox row may
select one receipt as the committed lifecycle outcome for its generation.
Every consumer must apply the same rule:

* delivery identity is event-scoped: ``(event, plan, adapter, channel)``;
* empty and absent channels are one identity, matching persistence semantics;
* outbox-backed receipts are current only when their exact outbox generation
  points at their ``receipt_id``;
* outbox generation rank comes from mutable outbox state, never receipt claims;
* outbox-less receipts remain eligible by durable append order; and
* after choosing one candidate per authority class, durable append order decides
  which class most recently changed observable lifecycle state.

This module is deliberately storage-agnostic. SQLite implements the same rule
in SQL and is checked against these pure functions by conformance tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Iterable, TypeVar

from medre.core.events.delivery import (
    DeliveryAttemptProvenance,
    normalize_replay_run_id,
)

__all__ = [
    "DeliveryAuthorityResolver",
    "DeliveryIdentity",
    "ReceiptAuthority",
    "ResolvedDeliverySnapshot",
    "authority_index",
    "committed_receipt_for_outbox",
    "delivery_identity",
    "delivery_attempt_provenance_mismatch",
    "delivery_identity_sort_key",
    "effective_generation",
    "group_outbox_by_identity",
    "group_receipts_by_identity",
    "receipt_kind",
    "select_current_outbox",
    "select_current_receipt",
]

_T = TypeVar("_T")


def _get(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalized_channel(value: Any) -> str | None:
    """Normalize the persisted no-channel representations to one identity."""
    if value is None or value == "":
        return None
    return str(value)


@dataclass(frozen=True, slots=True)
class DeliveryIdentity:
    """Full event-scoped identity of one logical delivery target.

    Empty and absent channels are the same lifecycle identity throughout
    storage and in-memory authority resolution, so normalize that distinction
    when the value object is constructed rather than at individual call sites.
    """

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_channel",
            _normalized_channel(self.target_channel),
        )

    @property
    def complete(self) -> bool:
        """Whether all required non-channel identity components are present."""
        return bool(self.event_id and self.delivery_plan_id and self.target_adapter)


@dataclass(frozen=True, slots=True)
class ReceiptAuthority:
    """Committed outbox pointers for one event-scoped delivery identity.

    Presence of this object means at least one outbox generation exists for the
    identity. ``committed`` may therefore be empty; that state is semantically
    different from having no outbox generations at all. Each tuple is
    ``(outbox_id, receipt_id, finalized_attempt_number)`` so receipt eligibility
    and generation ordering both derive from mutable outbox authority.
    """

    committed: frozenset[tuple[str, str, int]]

    def generation_for(self, receipt: Any) -> int | None:
        """Return committed generation for *receipt*, or ``None`` if ineligible."""
        outbox_id = str(_get(receipt, "outbox_id") or "")
        receipt_id = str(_get(receipt, "receipt_id") or "")
        if not outbox_id or not receipt_id:
            return None
        for committed_outbox, committed_receipt, attempt in self.committed:
            if committed_outbox == outbox_id and committed_receipt == receipt_id:
                return attempt
        return None


def delivery_identity(record: Any) -> DeliveryIdentity:
    """Build an event-scoped delivery identity from an object or mapping."""
    return DeliveryIdentity(
        str(_get(record, "event_id") or ""),
        str(_get(record, "delivery_plan_id") or ""),
        str(_get(record, "target_adapter") or ""),
        _normalized_channel(_get(record, "target_channel")),
    )


def delivery_identity_sort_key(identity: DeliveryIdentity) -> tuple[str, str, str, str]:
    """Return a deterministic sort key that handles the no-channel identity."""
    return (
        identity.event_id,
        identity.delivery_plan_id,
        identity.target_adapter,
        identity.target_channel or "",
    )


def authority_index(
    outbox_items: Iterable[Any],
) -> dict[DeliveryIdentity, ReceiptAuthority]:
    """Index exact committed outbox pointers by full delivery identity."""
    mutable: dict[DeliveryIdentity, set[tuple[str, str, int]]] = {}
    for item in outbox_items:
        identity = delivery_identity(item)
        committed = mutable.setdefault(identity, set())
        receipt_id = str(_get(item, "receipt_id") or "")
        outbox_id = str(_get(item, "outbox_id") or "")
        if receipt_id and outbox_id:
            committed.add(
                (
                    outbox_id,
                    receipt_id,
                    int(_get(item, "attempt_number") or 1),
                )
            )
    return {
        identity: ReceiptAuthority(frozenset(commits))
        for identity, commits in mutable.items()
    }


def group_receipts_by_identity(
    receipts: Iterable[_T],
) -> dict[DeliveryIdentity, list[_T]]:
    """Group immutable receipts by full event-scoped delivery identity."""
    grouped: dict[DeliveryIdentity, list[_T]] = {}
    for receipt in receipts:
        grouped.setdefault(delivery_identity(receipt), []).append(receipt)
    return grouped


def group_outbox_by_identity(
    outbox_items: Iterable[_T],
) -> dict[DeliveryIdentity, list[_T]]:
    """Group mutable outbox generations by full event-scoped identity."""
    grouped: dict[DeliveryIdentity, list[_T]] = {}
    for item in outbox_items:
        grouped.setdefault(delivery_identity(item), []).append(item)
    return grouped


def effective_generation(item: Any) -> int:
    """Return the reserved generation when present, else the finalized one."""
    return int(_get(item, "active_attempt") or _get(item, "attempt_number") or 1)


def delivery_attempt_provenance_mismatch(
    provenance: DeliveryAttemptProvenance,
    item: Any,
) -> str | None:
    """Return why *provenance* contradicts durable outbox authority, if at all.

    This helper validates an asynchronous callback envelope against the exact
    outbox row that admitted the attempt. Mutable row provenance is validation
    evidence only; callers must never use it to reconstruct a missing callback
    source or replay run.
    """
    outbox_id = str(_get(item, "outbox_id") or "")
    if outbox_id != provenance.outbox_id:
        return (
            f"outbox_id mismatch: callback={provenance.outbox_id!r} row={outbox_id!r}"
        )

    expected_identity = DeliveryIdentity(
        provenance.event_id,
        provenance.delivery_plan_id,
        provenance.target_adapter,
        provenance.target_channel,
    )
    actual_identity = delivery_identity(item)
    if actual_identity != expected_identity:
        return (
            "delivery identity mismatch: "
            f"callback={expected_identity!r} row={actual_identity!r}"
        )

    generation = effective_generation(item)
    if generation != provenance.attempt_number:
        return (
            "attempt generation mismatch: "
            f"callback={provenance.attempt_number} row={generation}"
        )

    row_source = _get(item, "dispatch_source")
    if row_source is not None and str(row_source) != provenance.source:
        return (
            "dispatch source mismatch: "
            f"callback={provenance.source!r} row={str(row_source)!r}"
        )

    row_run_id = normalize_replay_run_id(_get(item, "replay_run_id"))
    if row_run_id != provenance.replay_run_id:
        return (
            "replay_run_id mismatch: "
            f"callback={provenance.replay_run_id!r} row={row_run_id!r}"
        )
    return None


def _outbox_rank(item: Any) -> tuple[int, str, str, str]:
    """Rank operational generations without relying on incidental list order."""
    return (
        effective_generation(item),
        _iso(_get(item, "updated_at")),
        _iso(_get(item, "created_at")),
        str(_get(item, "outbox_id") or ""),
    )


def select_current_outbox(items: Iterable[_T]) -> _T | None:
    """Select the newest operational generation for one delivery identity."""
    item_list = list(items)
    return max(item_list, key=_outbox_rank) if item_list else None


def _append_rank(receipt: Any) -> tuple[int, str, str]:
    """Return durable append order for receipts within one authority class."""
    return (
        int(_get(receipt, "sequence") or 0),
        _iso(_get(receipt, "created_at")),
        str(_get(receipt, "receipt_id") or ""),
    )


def _generation_rank(
    receipt: Any,
    authority: ReceiptAuthority,
) -> tuple[int, int, str, str]:
    """Rank committed evidence by outbox generation then append order."""
    generation = authority.generation_for(receipt)
    if generation is None:
        raise ValueError("receipt is not committed by this authority")
    return (generation, *_append_rank(receipt))


def select_current_receipt(
    receipts: Iterable[_T],
    authority: ReceiptAuthority | None,
) -> _T | None:
    """Select lifecycle-authoritative receipt under the shared current rule.

    ``authority is None`` means no outbox generation exists, so immutable append
    order is authoritative. If outbox generations exist, an outbox-backed
    receipt is eligible only when one of those generations commits its ID.
    Outbox-less receipts remain eligible. The latest eligible receipt wins.
    """
    receipt_list = list(receipts)
    if authority is None:
        return max(receipt_list, key=_append_rank) if receipt_list else None

    outbox_backed = [
        receipt
        for receipt in receipt_list
        if authority.generation_for(receipt) is not None
    ]
    outboxless = [receipt for receipt in receipt_list if not _get(receipt, "outbox_id")]

    # Multiple outbox generations are ordered by dispatch identity, never by a
    # late append from an older generation. Outbox-less evidence retains its
    # append-order contract. Once each authority class has one candidate,
    # durable append order decides which class most recently changed the
    # delivery's observable lifecycle state.
    candidates: list[_T] = []
    if outbox_backed:
        candidates.append(
            max(outbox_backed, key=lambda receipt: _generation_rank(receipt, authority))
        )
    if outboxless:
        candidates.append(max(outboxless, key=_append_rank))
    return max(candidates, key=_append_rank) if candidates else None


@dataclass(frozen=True, slots=True)
class ResolvedDeliverySnapshot(Generic[_T]):
    """Resolved read model for one logical delivery identity.

    The snapshot keeps immutable history and mutable lifecycle authority in one
    value so diagnostics and operator projections do not have to repeat the
    receipt/outbox join. ``authoritative_receipt`` answers global immutable
    lifecycle authority, ``current_outbox`` answers current operational state,
    ``current_receipt`` is the exact receipt committed by that current mutable
    generation, ``current_attempt`` resolves its transport-attempt evidence,
    ``latest_attempt`` answers the newest immutable transport execution whether
    committed or historical, and ``causative_receipt`` resolves global lifecycle
    authority back to the attempt it names when loaded.
    """

    identity: DeliveryIdentity
    receipts: tuple[_T, ...]
    outbox_items: tuple[Any, ...]
    authoritative_receipt: _T | None
    current_outbox: Any | None
    current_receipt: _T | None
    current_attempt: _T | None
    latest_attempt: _T | None
    causative_receipt: _T | None


def receipt_kind(record: Any) -> str:
    """Return explicit receipt kind, inferring legacy mapping inputs by status."""
    kind = str(_get(record, "receipt_kind") or "")
    if kind in {"attempt", "lifecycle"}:
        return kind
    return (
        "attempt"
        if str(_get(record, "status") or "") in {"queued", "sent", "failed"}
        else "lifecycle"
    )


def _latest_attempt(receipts: Iterable[_T]) -> _T | None:
    """Return the highest-numbered attempt, breaking ties by append order.

    Return ``None`` when the history contains no attempt receipt.
    """
    attempts = [receipt for receipt in receipts if receipt_kind(receipt) == "attempt"]
    if not attempts:
        return None
    return max(
        attempts,
        key=lambda receipt: (
            int(_get(receipt, "attempt_number") or 1),
            *_append_rank(receipt),
        ),
    )


def committed_receipt_for_outbox(
    outbox: Any | None,
    receipts: Iterable[_T],
) -> _T | None:
    """Return the receipt committed by the exact supplied outbox generation.

    An outbox can retain a pointer to the previously finalized attempt
    while a retry reservation is active.  Therefore the pointer is eligible
    only when the pointed receipt also belongs to the effective generation
    (``active_attempt`` while reserved, otherwise ``attempt_number``).
    """
    if outbox is None:
        return None
    outbox_id = str(_get(outbox, "outbox_id") or "")
    receipt_id = str(_get(outbox, "receipt_id") or "")
    if not outbox_id or not receipt_id:
        return None
    generation = effective_generation(outbox)
    return next(
        (
            receipt
            for receipt in receipts
            if str(_get(receipt, "outbox_id") or "") == outbox_id
            and str(_get(receipt, "receipt_id") or "") == receipt_id
            and int(_get(receipt, "attempt_number") or 1) == generation
        ),
        None,
    )


def _current_generation_attempt(
    current_outbox: Any | None,
    current_receipt: _T | None,
    receipts: Iterable[_T],
) -> _T | None:
    """Resolve committed attempt evidence for the current mutable generation.

    Attempt authority can be direct (queued/sent/failed pointer) or indirect
    through a committed lifecycle receipt whose parent is the causative failed
    attempt.  Receipts not reachable from the current outbox pointer are never
    promoted merely because they claim the same ``outbox_id`` and generation.
    """
    if current_outbox is None or current_receipt is None:
        return None
    if receipt_kind(current_receipt) == "attempt":
        return current_receipt

    parent_id = str(_get(current_receipt, "parent_receipt_id") or "")
    if not parent_id:
        return None
    outbox_id = str(_get(current_outbox, "outbox_id") or "")
    generation = effective_generation(current_outbox)
    return next(
        (
            receipt
            for receipt in receipts
            if receipt_kind(receipt) == "attempt"
            and str(_get(receipt, "receipt_id") or "") == parent_id
            and str(_get(receipt, "outbox_id") or "") == outbox_id
            and int(_get(receipt, "attempt_number") or 1) == generation
        ),
        None,
    )


def _causative_receipt(
    authoritative_receipt: _T | None,
    receipts: Iterable[_T],
) -> _T | None:
    """Find the loaded parent of an authoritative lifecycle receipt, if any.

    Return ``None`` for attempt authority, missing parent IDs, or parents absent
    from the supplied history.
    """
    if (
        authoritative_receipt is None
        or receipt_kind(authoritative_receipt) != "lifecycle"
    ):
        return None
    parent_id = str(_get(authoritative_receipt, "parent_receipt_id") or "")
    if not parent_id:
        return None
    return next(
        (
            receipt
            for receipt in receipts
            if str(_get(receipt, "receipt_id") or "") == parent_id
        ),
        None,
    )


class DeliveryAuthorityResolver(Generic[_T]):
    """Resolve current delivery evidence from immutable history + outbox state.

    The resolver is the in-memory authority boundary for projections,
    diagnostics, recovery, and evidence generation.  It deliberately keeps
    receipt grouping and outbox-pointer filtering together so higher-level
    consumers cannot accidentally apply only half of the rule.
    """

    __slots__ = ("_authorities", "_outbox", "_receipts")

    def __init__(
        self,
        receipts: Iterable[_T] = (),
        outbox_items: Iterable[Any] = (),
    ) -> None:
        receipt_list = list(receipts)
        outbox_list = list(outbox_items)
        self._receipts = {
            identity: tuple(group)
            for identity, group in group_receipts_by_identity(receipt_list).items()
        }
        self._outbox = {
            identity: tuple(group)
            for identity, group in group_outbox_by_identity(outbox_list).items()
        }
        self._authorities = authority_index(outbox_list)

    @property
    def identities(self) -> frozenset[DeliveryIdentity]:
        """All identities represented by receipt history or outbox state."""
        return (
            frozenset(self._receipts)
            | frozenset(self._outbox)
            | frozenset(self._authorities)
        )

    def ordered_identities(self) -> tuple[DeliveryIdentity, ...]:
        """Return represented identities in deterministic display order."""
        return tuple(sorted(self.identities, key=delivery_identity_sort_key))

    def receipts_for(self, identity: DeliveryIdentity) -> tuple[_T, ...]:
        """Return immutable receipt history for *identity* in caller order."""
        return self._receipts.get(identity, ())

    def authority_for(self, identity: DeliveryIdentity) -> ReceiptAuthority | None:
        """Return outbox authority for *identity*, if any generation exists."""
        return self._authorities.get(identity)

    def outbox_for(self, identity: DeliveryIdentity) -> tuple[Any, ...]:
        """Return persisted outbox generations for *identity*."""
        return self._outbox.get(identity, ())

    def current_outbox(self, identity: DeliveryIdentity) -> Any | None:
        """Return the current operational generation for *identity*."""
        return select_current_outbox(self.outbox_for(identity))

    def current(self, identity: DeliveryIdentity) -> _T | None:
        """Return the lifecycle-authoritative receipt for *identity*."""
        return select_current_receipt(
            self.receipts_for(identity),
            self.authority_for(identity),
        )

    def resolve(self, identity: DeliveryIdentity) -> ResolvedDeliverySnapshot[_T]:
        """Return one identity's history, current outbox, and receipt authority.

        The snapshot also includes the latest attempt and the loaded parent of
        the authoritative lifecycle receipt, when those receipts exist.
        """
        receipts = self.receipts_for(identity)
        outbox_items = self.outbox_for(identity)
        authority = self.authority_for(identity)
        authoritative_receipt = select_current_receipt(receipts, authority)
        current_outbox = select_current_outbox(outbox_items)
        current_receipt = committed_receipt_for_outbox(current_outbox, receipts)
        latest_attempt = _latest_attempt(receipts)
        return ResolvedDeliverySnapshot(
            identity=identity,
            receipts=receipts,
            outbox_items=outbox_items,
            authoritative_receipt=authoritative_receipt,
            current_outbox=current_outbox,
            current_receipt=current_receipt,
            current_attempt=_current_generation_attempt(
                current_outbox, current_receipt, receipts
            ),
            latest_attempt=latest_attempt,
            causative_receipt=_causative_receipt(authoritative_receipt, receipts),
        )

    def ordered_snapshots(self) -> tuple[ResolvedDeliverySnapshot[_T], ...]:
        """Return all represented deliveries as deterministic resolved snapshots."""
        return tuple(self.resolve(identity) for identity in self.ordered_identities())
