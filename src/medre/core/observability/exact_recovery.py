"""Fail-closed, read-only selection of one persisted delivery authority.

This is a read-only observation, not a replay execution grant. A future write path
MUST revalidate the exact identity and generation under storage authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from medre.core.delivery_authority import (
    DeliveryAuthorityResolver,
    DeliveryIdentity,
    ResolvedDeliverySnapshot,
    effective_generation,
)


class ExactPreviewError(ValueError):
    """An exact delivery selection was absent, ambiguous or invalidated."""


@dataclass(frozen=True, slots=True)
class ExactDeliverySelector:
    """One event/plan/adapter/channel, including the explicit no-channel case."""

    identity: DeliveryIdentity

    def __post_init__(self) -> None:
        if not self.identity.complete:
            raise ValueError("Exact recovery requires event, plan and adapter identity")


def exact_delivery_preview(
    authority: DeliveryAuthorityResolver[Any], selector: ExactDeliverySelector
) -> dict[str, Any]:
    """Bind read-only preview to the current durable generation. Never widen.

    A fingerprint describes the observed state only. It is not proof that the
    state will remain unchanged, and cannot authorize side effects.
    """
    matches: list[ResolvedDeliverySnapshot[Any]] = [
        snapshot
        for snapshot in authority.ordered_snapshots()
        if snapshot.identity == selector.identity
    ]
    if len(matches) != 1:
        raise ExactPreviewError(
            "Exact recovery target is absent or ambiguous; no action authorized"
        )
    snapshot = matches[0]
    outbox = snapshot.current_outbox
    receipt = (
        snapshot.current_receipt
        if outbox is not None
        else snapshot.authoritative_receipt
    )
    identity = selector.identity
    statement: dict[str, Any] = {
        "event_id": identity.event_id,
        "delivery_plan_id": identity.delivery_plan_id,
        "target_adapter": identity.target_adapter,
        "target_channel": identity.target_channel,
        "outbox_id": getattr(outbox, "outbox_id", None),
        "outbox_status": getattr(outbox, "status", None),
        "attempt_number": (
            effective_generation(outbox)
            if outbox is not None
            else getattr(receipt, "attempt_number", None)
        ),
        "receipt_id": getattr(receipt, "receipt_id", None),
        "receipt_status": getattr(receipt, "status", None),
    }
    fingerprint = hashlib.sha256(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "authority": statement,
        "observed_fingerprint": fingerprint,
        "excluded_sibling_count": len(authority.ordered_snapshots()) - 1,
        "execution_authorized": False,
        "scope": "read_only_current_generation",
    }
