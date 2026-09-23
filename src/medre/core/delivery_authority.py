"""Shared current-delivery authority resolution.

The immutable receipt log records history, while a delivery outbox row may
select one receipt as the committed lifecycle outcome for its generation.
Every consumer must apply the same rule:

* delivery identity is event-scoped: ``(event, plan, adapter, channel)``;
* empty and absent channels are one identity, matching persistence semantics;
* outbox-backed receipts are current only when a matching outbox generation
  points at their ``receipt_id``;
* outbox-less receipts remain eligible by durable append order; and
* across multiple generations, the latest eligible durable receipt wins.

This module is deliberately storage-agnostic. SQLite implements the same rule
in SQL and is checked against these pure functions by conformance tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Iterable, NamedTuple, TypeVar

__all__ = [
    "DeliveryAuthorityResolver",
    "DeliveryIdentity",
    "ReceiptAuthority",
    "authority_index",
    "delivery_identity",
    "delivery_identity_sort_key",
    "group_receipts_by_identity",
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


class DeliveryIdentity(NamedTuple):
    """Full event-scoped identity of one logical delivery target."""

    event_id: str
    delivery_plan_id: str
    target_adapter: str
    target_channel: str | None

    @property
    def complete(self) -> bool:
        """Whether all required non-channel identity components are present."""
        return bool(self.event_id and self.delivery_plan_id and self.target_adapter)


@dataclass(frozen=True, slots=True)
class ReceiptAuthority:
    """Outbox authority state for one event-scoped delivery identity.

    Presence of this object means at least one outbox generation exists for the
    identity. ``committed_receipt_ids`` may therefore be empty; that state is
    semantically different from having no outbox generations at all.
    """

    committed_receipt_ids: frozenset[str]


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

def authority_index(outbox_items: Iterable[Any]) -> dict[DeliveryIdentity, ReceiptAuthority]:
    """Index committed outbox receipt pointers by full delivery identity."""
    mutable: dict[DeliveryIdentity, set[str]] = {}
    for item in outbox_items:
        identity = delivery_identity(item)
        committed = mutable.setdefault(identity, set())
        receipt_id = _get(item, "receipt_id")
        if receipt_id:
            committed.add(str(receipt_id))
    return {
        identity: ReceiptAuthority(frozenset(receipt_ids))
        for identity, receipt_ids in mutable.items()
    }


def group_receipts_by_identity(
    receipts: Iterable[_T],
) -> dict[DeliveryIdentity, list[_T]]:
    """Group immutable receipts by full event-scoped delivery identity."""
    grouped: dict[DeliveryIdentity, list[_T]] = {}
    for receipt in receipts:
        grouped.setdefault(delivery_identity(receipt), []).append(receipt)
    return grouped


def _receipt_rank(receipt: Any) -> tuple[int, str, str]:
    """Return an ascending rank whose maximum is the latest durable receipt."""
    return (
        int(_get(receipt, "sequence") or 0),
        _iso(_get(receipt, "created_at")),
        str(_get(receipt, "receipt_id") or ""),
    )


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
        eligible = receipt_list
    else:
        committed = authority.committed_receipt_ids
        eligible = [
            receipt
            for receipt in receipt_list
            if not _get(receipt, "outbox_id")
            or str(_get(receipt, "receipt_id") or "") in committed
        ]
    return max(eligible, key=_receipt_rank) if eligible else None


class DeliveryAuthorityResolver(Generic[_T]):
    """Resolve current delivery evidence from immutable history + outbox state.

    The resolver is the in-memory authority boundary for projections,
    diagnostics, recovery, and evidence generation.  It deliberately keeps
    receipt grouping and outbox-pointer filtering together so higher-level
    consumers cannot accidentally apply only half of the rule.
    """

    __slots__ = ("_authorities", "_receipts")

    def __init__(
        self,
        receipts: Iterable[_T] = (),
        outbox_items: Iterable[Any] = (),
    ) -> None:
        self._receipts = {
            identity: tuple(group)
            for identity, group in group_receipts_by_identity(receipts).items()
        }
        self._authorities = authority_index(outbox_items)

    @property
    def identities(self) -> frozenset[DeliveryIdentity]:
        """All identities represented by receipt history or outbox state."""
        return frozenset(self._receipts) | frozenset(self._authorities)

    def ordered_identities(self) -> tuple[DeliveryIdentity, ...]:
        """Return represented identities in deterministic display order."""
        return tuple(sorted(self.identities, key=delivery_identity_sort_key))

    def receipts_for(self, identity: DeliveryIdentity) -> tuple[_T, ...]:
        """Return immutable receipt history for *identity* in caller order."""
        return self._receipts.get(identity, ())

    def authority_for(self, identity: DeliveryIdentity) -> ReceiptAuthority | None:
        """Return outbox authority for *identity*, if any generation exists."""
        return self._authorities.get(identity)

    def current(self, identity: DeliveryIdentity) -> _T | None:
        """Return the lifecycle-authoritative receipt for *identity*."""
        return select_current_receipt(
            self.receipts_for(identity),
            self.authority_for(identity),
        )
