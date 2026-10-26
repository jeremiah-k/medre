"""Read-only radio tail assessment; this module never grants native submission.

A positive assessment identifies evidence worth an operator's review, not a
permitted replay. Only the owned storage continuation transaction can validate
historical provenance against a newer independent dispatch claim.
"""

from __future__ import annotations

import msgspec

from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import (
    RadioLocalHandoff,
    plain_channel_payload_fingerprint,
)


def assess_radio_continuation(checkpoint: StoredRadioAttempt) -> dict[str, object]:
    """Describe a possible accepted-prefix/unsent-tail transfer without mutation.

    Decode all accepted local handoffs, requiring a common payload fingerprint.
    The result deliberately does not authorize sending: origin rows, current
    parent identity, route semantics, current worker and lease are checked in
    the storage transaction and at each native grant, not by this report.
    """
    if not isinstance(checkpoint, StoredRadioAttempt):
        raise TypeError("radio assessment requires a retained typed checkpoint")
    parts = checkpoint.progress.parts
    accepted = sum(part.state == "accepted" for part in parts)
    unsent = sum(part.state == "unsent" for part in parts)
    reasons: list[str] = []
    if not accepted:
        reasons.append("no_accepted_prefix")
    if not unsent:
        reasons.append("no_unsent_tail")
    if any(part.state in {"unknown", "submitting"} for part in parts):
        reasons.append("uncertain_native_submission")
    channel = checkpoint.provenance.target_channel
    fingerprints: set[str] = set()
    for part in parts:
        if part.state != "accepted":
            continue
        try:
            fact = RadioLocalHandoff.from_part(part, checkpoint.provenance)
        except (ValueError, TypeError, msgspec.DecodeError):
            reasons.append("invalid_accepted_handoff")
            break
        if fact.native_channel_id is not None and fact.native_channel_id != channel:
            if "native_channel_scope_mismatch" not in reasons:
                reasons.append("native_channel_scope_mismatch")
        if fact.payload_fingerprint is None:
            reasons.append("missing_native_payload_proof")
            break
        fingerprints.add(fact.payload_fingerprint)
    if len(fingerprints) > 1:
        reasons.append("inconsistent_native_payload_proof")
    # Current continuation permits only canonical numeric channels. Even a
    # matching group of fingerprints cannot attest a different channel scope.
    if (
        channel is None
        or not channel.isascii()
        or not channel.isdecimal()
        or len(channel) > 32
        or str(int(channel)) != channel
    ):
        reasons.append("unsupported_native_channel_scope")
    elif fingerprints and fingerprints != {
        plain_channel_payload_fingerprint(int(channel))
    }:
        reasons.append("native_channel_payload_mismatch")
    return {
        "status": "review_candidate" if not reasons else "held",
        "accepted_parts": accepted,
        "unsent_parts": unsent,
        "total_parts": len(parts),
        "reasons": reasons,
        "requires": [
            "independent_new_outbox_claim_and_current_worker_lease",
            "exact_latest_predecessor_and_original_acceptance_validation",
            "matching_original_plain_channel_native_semantics",
            "authorized_runtime_dispatch_through_the_paced_native_owner",
        ],
        "native_submission_authorized": False,
    }
