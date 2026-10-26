"""Typed local handoff references with immutable original-attempt attribution.

These references are retained facts, never submission grants. A continuation
must resolve each origin against owned storage before inheriting an acceptance.
Legacy local references without an origin belong to their containing attempt.
"""

from __future__ import annotations

from typing import Annotated

import msgspec

from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.events.delivery import (
    DeliveryAttemptProvenance,
    DeliveryConfirmationLevel,
)
from medre.core.rendering.radio_progress import RadioPartProgress


class RadioHandoffOrigin(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """The original ledger row that observed this part's local native acceptance."""

    outbox_id: Annotated[str, msgspec.Meta(min_length=1, max_length=1024)]
    attempt_number: Annotated[int, msgspec.Meta(ge=1)]


class RadioLocalHandoff(
    msgspec.Struct, frozen=True, forbid_unknown_fields=True, omit_defaults=True
):
    """A packet-scoped handoff, optionally inherited from an earlier attempt.

    Native ID null remains meaningful for transports that assign no packet ID.
    Confirmation strength does not increase when evidence changes claim owner.
    """

    ordinal: Annotated[int, msgspec.Meta(ge=1, le=128)]
    native_message_id: str | None
    native_channel_id: str | None
    confirmation_level: DeliveryConfirmationLevel
    origin: RadioHandoffOrigin | None = None
    payload_fingerprint: (
        Annotated[str, msgspec.Meta(pattern="^[0-9a-f]{64}$")] | None
    ) = None

    @classmethod
    def from_part(
        cls, part: RadioPartProgress, provenance: DeliveryAttemptProvenance
    ) -> RadioLocalHandoff:
        """Decode a committed local fact and retain its first observing attempt."""
        if part.state != "accepted" or part.handoff_id is None:
            raise ValueError("radio continuation requires accepted local handoffs")
        value = msgspec.json.decode(part.handoff_id, type=cls)
        if value.ordinal != part.ordinal or provenance.outbox_id is None:
            raise ValueError("radio handoff ordinal or original claim is invalid")
        return msgspec.structs.replace(
            value,
            origin=value.origin
            or RadioHandoffOrigin(provenance.outbox_id, provenance.attempt_number),
        )

    def encode(self) -> str:
        """Serialize the bounded part reference without changing native identity."""
        return msgspec.json.encode(self).decode()

    def handoff(self) -> AdapterHandoffResult:
        """Recover only the declared local handoff strength for whole-plan aggregation."""
        return AdapterHandoffResult(
            native_message_id=self.native_message_id,
            native_channel_id=self.native_channel_id,
            confirmation_level=self.confirmation_level,
        )
