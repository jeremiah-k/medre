"""Pure, read-only uncertain-handoff verdict tests.

Packet, message, identity, and provenance evidence are classified without a
storage backend or delivery lifecycle worker.
"""

from __future__ import annotations

import pytest

from medre.core.evidence.handoff_resolution import resolve_uncertain_handoff
from medre.core.rendering.radio_progress import RadioAttemptProgress, RadioPartProgress
from medre.core.storage.radio import StoredRadioAttempt
from tests.helpers.handoff_resolution import (
    _checkpoint,
    _identity,
    _observation,
    _plan,
    _provenance,
)

# ---------------------------------------------------------------------------
# Pure verdict tests
# ---------------------------------------------------------------------------


def test_complete_radio_handoff_settles_delivered_with_evidence() -> None:
    """A fenced checkpoint whose whole plan was handed off settles delivered."""
    provenance = _provenance("obox-complete", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[],
    )
    assert resolution.verdict == "delivered_with_evidence"
    assert resolution.uncertain is False
    assert resolution.has_checkpoint is True
    assert resolution.accepted_parts == 3


def test_empty_grant_fence_settles_definitively_not_submitted() -> None:
    """A bound plan whose grant fence never committed proves non-submission."""
    provenance = _provenance("obox-unsent", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["unsent", "unsent", "unsent"]),
        observations=[],
    )
    assert resolution.verdict == "definitively_not_submitted"
    assert resolution.uncertain is False


@pytest.mark.parametrize("boundary_state", ["submitting", "unknown"])
def test_uncertain_boundary_stays_unresolved(boundary_state: str) -> None:
    """An interrupted native call is unresolved, never guessed either way."""
    provenance = _provenance("obox-uncertain", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", boundary_state, "unsent"]),
        observations=[],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert resolution.accepted_parts == 1
    assert resolution.uncertain_parts == 1
    assert resolution.unsent_parts == 1


def test_checkpoint_identity_contradiction_stays_held() -> None:
    """A checkpoint naming another delivery never settles this attempt."""
    foreign = _provenance("obox-foreign", 1, event_id="evt-other")
    resolution = resolve_uncertain_handoff(
        outbox_id=foreign.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(foreign, ["accepted", "accepted", "accepted"]),
        observations=[],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "checkpoint_identity_mismatch" in resolution.reasons


def test_provider_accepted_observation_settles_message_delivered() -> None:
    """An adapter-reported provider acceptance proves a receipt-less send."""
    provenance = _provenance("obox-message", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[
            _observation("obs-positive", provenance, adapter_message_id="native-9")
        ],
    )
    assert resolution.verdict == "delivered_with_evidence"
    assert resolution.uncertain is False
    assert resolution.has_checkpoint is False


def test_local_only_confirmation_never_settles_delivered() -> None:
    """A local queue/transport echo is not provider acceptance evidence."""
    provenance = _provenance("obox-local-only", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[
            _observation("obs-local", provenance, confirmation_level="local_transport")
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is False


def test_negative_observation_keeps_outcome_unresolved() -> None:
    """A post-handoff failure report cannot prove or disprove acceptance."""
    provenance = _provenance("obox-negative", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[
            _observation(
                "obs-failed", provenance, state="failed", adapter_message_id=None
            )
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True


def test_conflicting_observations_stay_held() -> None:
    """Competing positive and negative reports keep the result held."""
    provenance = _provenance("obox-conflict", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[
            _observation("obs-positive", provenance),
            _observation("obs-negative", provenance, state="rejected"),
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True


def test_foreign_observation_is_ignored_not_trusted() -> None:
    """An observation naming another attempt never settles this one."""
    provenance = _provenance("obox-foreign-obs", 1)
    foreign = _provenance("obox-foreign-obs", 2)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[_observation("obs-other-attempt", foreign)],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is False


def test_packet_observation_without_checkpoint_is_not_message_evidence() -> None:
    """Packet-scoped facts require their retained checkpoint correlation."""
    provenance = _provenance("obox-no-checkpoint", 1)
    packet_observation = _observation(
        "obs-packet",
        provenance,
        radio_packet={
            "ordinal": 1,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": 1,
        },
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=[packet_observation],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is False


def test_packet_observation_cannot_upgrade_an_uncertain_part() -> None:
    """Existing interfaces cannot admit evidence for a non-accepted part.

    Storage admits radio packet observations only against a retained original
    acceptance, so a packet fact cannot resolve the uncertain boundary part
    today. Even if such a row existed, the resolver must not treat it as
    whole-plan acceptance.
    """
    provenance = _provenance("obox-upgrade", 1)
    packet_observation = _observation(
        "obs-uncertain-part",
        provenance,
        radio_packet={
            "ordinal": 2,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": 1,
        },
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "unknown", "unsent"]),
        observations=[packet_observation],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True


def test_negative_packet_observation_conflicts_with_complete_handoff() -> None:
    """A retained acceptance contradicted by a packet report stays held."""
    provenance = _provenance("obox-handoff-conflict", 1)
    negative_packet = _observation(
        "obs-neg-packet",
        provenance,
        state="failed",
        radio_packet={
            "ordinal": 1,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": 1,
        },
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[negative_packet],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert resolution.reasons == ("conflicting_packet_observation",)


def test_positive_packet_observation_agrees_with_complete_handoff() -> None:
    """A qualifying packet ACK does not block a complete fenced handoff."""
    provenance = _provenance("obox-handoff-agree", 1)
    positive_packet = _observation(
        "obs-pos-packet",
        provenance,
        confirmation_level="remote_service",
        radio_packet={
            "ordinal": 1,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": 1,
        },
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[positive_packet],
    )
    assert resolution.verdict == "delivered_with_evidence"
    assert resolution.uncertain is False


def test_whole_message_failure_conflicts_with_complete_handoff() -> None:
    """A whole-message failure report holds a fully accepted checkpoint."""
    provenance = _provenance("obox-message-conflict", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[
            _observation(
                "obs-message-failed",
                provenance,
                state="failed",
                adapter_message_id=None,
            )
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "conflicting_message_observation" in resolution.reasons


def test_checkpoint_from_another_replay_run_never_settles() -> None:
    """A checkpoint bound under a different replay run never settles the row."""
    provenance = _provenance(
        "obox-run-mismatch", 1, source="retry", replay_run_id="run-a"
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[],
        attempt_source="retry",
        replay_run_id="run-b",
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "checkpoint_provenance_incoherent" in resolution.reasons


@pytest.mark.parametrize(
    ("row_source", "checkpoint_source", "expected"),
    [
        ("replay", "retry", "delivered_with_evidence"),
        ("live", "retry", "delivered_with_evidence"),
        ("retry", "retry", "delivered_with_evidence"),
        ("retry", "replay", "unresolved"),
    ],
)
def test_checkpoint_dispatch_source_matches_replay_origin(
    row_source, checkpoint_source, expected
) -> None:
    provenance = _provenance("obox-source-coherence", 1, source=checkpoint_source)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[],
        attempt_source=row_source,
    )
    assert resolution.verdict == expected


def test_live_row_never_settles_from_a_replay_lineage_checkpoint() -> None:
    """A live row cannot be settled by a checkpoint bound for a replay run."""
    provenance = _provenance("obox-live-mismatch", 1, source="replay")
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[],
        attempt_source="live",
        replay_run_id=None,
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "checkpoint_provenance_incoherent" in resolution.reasons


@pytest.mark.parametrize("with_acceptance", [False, True])
def test_malformed_packet_claim_is_never_whole_message_evidence(
    with_acceptance: bool,
) -> None:
    """Unreadable evidence blocks a verdict even beside qualifying acceptance."""
    provenance = _provenance("obox-bad-packet", 1)
    observations = [_observation("obs-bad-packet", provenance, radio_packet="packet 1")]
    if with_acceptance:
        observations.append(_observation("obs-whole-message", provenance))
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=None,
        observations=observations,
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "unreadable_packet_evidence" in resolution.reasons


def test_malformed_packet_claim_holds_a_complete_handoff() -> None:
    """An uncorrelatable packet claim keeps a fully accepted checkpoint held."""
    provenance = _provenance("obox-unreadable-packet", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "accepted", "accepted"]),
        observations=[
            _observation(
                "obs-uncorrelatable",
                provenance,
                radio_packet={
                    "ordinal": 1,
                    "origin_outbox_id": "obox-elsewhere",
                    "origin_attempt_number": 1,
                },
            )
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert "unreadable_packet_evidence" in resolution.reasons


@pytest.mark.parametrize(
    "conflict", ["malformed_packet", "unsent_packet", "acceptance"]
)
def test_evidence_can_contradict_an_empty_fence(conflict: str) -> None:
    provenance = _provenance("obox-fence-conflict", 1)
    radio_packet = {
        "malformed_packet": {"ordinal": "one"},
        "unsent_packet": {
            "ordinal": 1,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": 1,
        },
        "acceptance": None,
    }[conflict]
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["unsent", "unsent", "unsent"]),
        observations=[
            _observation("obs-fence-conflict", provenance, radio_packet=radio_packet)
        ],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.uncertain is True
    assert resolution.reasons == (
        (
            "provider_acceptance_conflicts_with_empty_fence"
            if conflict == "acceptance"
            else "unreadable_packet_evidence"
        ),
    )


def test_corrupt_accepted_handoff_reference_cannot_settle() -> None:
    provenance = _provenance("obox-bad-reference", 1)
    progress = RadioAttemptProgress(
        provenance.outbox_id,
        1,
        _plan(),
        tuple(RadioPartProgress(i, "accepted", "invalid") for i in range(1, 4)),
    )
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=StoredRadioAttempt(provenance, progress, 6),
        observations=[],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.reasons == ("unreadable_handoff_reference",)
