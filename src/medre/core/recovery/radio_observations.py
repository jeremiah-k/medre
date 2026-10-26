"""Read-only packet observation projection over an owned radio checkpoint.

Transport observations remain separate from delivery lifecycle receipts. A
packet may only be correlated to an accepted native handoff at its *original*
attempt; later retry generations do not change that identity. The caller must
obtain the checkpoint from owned storage, not from an untrusted JSON payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import msgspec

from medre.core.events import DeliveryObservation
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioLocalHandoff

RadioObservationStatus = Literal[
    "unobserved", "partial", "all_packets_observed", "held"
]
_ORDER = {"remote_service": 0, "end_to_end": 1}


@dataclass(frozen=True, slots=True)
class RadioObservationSummary:
    """Derived evidence only; never an outbox transition or send grant."""

    total_parts: int
    accepted_parts: int
    observed_parts: int
    status: RadioObservationStatus
    observation_floor: str | None = None
    reason: str | None = None
    native_submission_authorized: bool = False


def project_radio_packet_observations(
    checkpoint: StoredRadioAttempt,
    observations: Sequence[DeliveryObservation],
) -> RadioObservationSummary:
    """Correlate exact packet ACKs without promoting their parent lifecycle.

    A packet observation must identify ordinal and original attempt in its
    ``metadata.radio_packet`` object, and must match the native packet ID,
    native channel, event, plan and adapter. Generic message observations do
    not qualify. Competing positive/negative reports keep the result held.
    """
    parts = checkpoint.progress.parts
    accepted = [part for part in parts if part.state == "accepted"]

    def result(
        status: RadioObservationStatus,
        observed: int = 0,
        *,
        level: str | None = None,
        reason: str | None = None,
    ) -> RadioObservationSummary:
        """Construct a read-only summary with counts tied to this retained checkpoint."""
        return RadioObservationSummary(
            len(parts), len(accepted), observed, status, level, reason
        )

    if any(part.state in {"submitting", "unknown"} for part in parts):
        return result("held", reason="uncertain_native_submission")

    facts: dict[int, RadioLocalHandoff] = {}
    for part in accepted:
        try:
            facts[part.ordinal] = RadioLocalHandoff.from_part(
                part, checkpoint.provenance
            )
            origin = facts[part.ordinal].origin
            if (
                origin is None
                or origin.outbox_id != checkpoint.provenance.outbox_id
                or origin.attempt_number > checkpoint.provenance.attempt_number
            ):
                return result("held", reason="invalid_accepted_handoff")
        except (ValueError, TypeError, msgspec.DecodeError):
            return result("held", reason="invalid_accepted_handoff")

    # Native packet IDs are the only transport-side correlation handle.
    # Reuse within one retained plan makes an ACK ambiguous even when a
    # caller supplies distinct ordinal metadata.
    native_keys = [
        (fact.native_channel_id, fact.native_message_id)
        for fact in facts.values()
        if fact.native_message_id is not None
    ]
    if len(native_keys) != len(set(native_keys)):
        return result("held", reason="ambiguous_native_packet_identity")

    observed: dict[int, set[str]] = {}
    rejected: set[int] = set()
    seen_observations: dict[str, DeliveryObservation] = {}
    for observation in observations:
        previous = seen_observations.setdefault(observation.observation_id, observation)
        if previous != observation:
            return result("held", reason="conflicting_observation_identity")
        payload = observation.metadata.get("radio_packet")
        if not isinstance(payload, Mapping):
            continue
        ordinal = payload.get("ordinal")
        origin_outbox = payload.get("origin_outbox_id")
        origin_attempt = payload.get("origin_attempt_number")
        if type(ordinal) is not int or ordinal not in facts:
            continue
        fact = facts[ordinal]
        origin = fact.origin
        if origin is None:
            return result("held", reason="invalid_accepted_handoff")
        if (
            type(origin_attempt) is not int
            or type(observation.attempt_number) is not int
            or origin_outbox != origin.outbox_id
            or origin_attempt != origin.attempt_number
            or observation.outbox_id != origin.outbox_id
            or observation.attempt_number != origin.attempt_number
            or observation.event_id != checkpoint.provenance.event_id
            or observation.delivery_plan_id != checkpoint.provenance.delivery_plan_id
            or observation.target_adapter != checkpoint.provenance.target_adapter
            or observation.target_channel != checkpoint.provenance.target_channel
            or fact.native_message_id is None
            or observation.adapter_message_id != fact.native_message_id
            or observation.native_channel_id != fact.native_channel_id
        ):
            continue
        if observation.state in {"failed", "rejected", "cancelled"}:
            rejected.add(ordinal)
        elif (
            observation.state == "delivered"
            and observation.confirmation_level in _ORDER
        ):
            observed.setdefault(ordinal, set()).add(observation.confirmation_level)

    count = len(observed)
    if rejected:
        return result(
            "held", count, reason="conflicting_or_negative_packet_observation"
        )
    if len(accepted) != len(parts):
        return result(
            "partial" if count else "unobserved",
            count,
            reason="local_handoff_incomplete",
        )
    if count != len(parts):
        return result(
            "partial" if count else "unobserved",
            count,
            reason="packet_observations_incomplete",
        )
    floor = min(
        (level for levels in observed.values() for level in levels),
        key=_ORDER.__getitem__,
    )
    return result("all_packets_observed", count, level=floor)
