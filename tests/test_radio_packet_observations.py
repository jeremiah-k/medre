"""Packet observation correlation never creates lifecycle or send authority."""

from __future__ import annotations

from dataclasses import replace

import msgspec
import pytest

from medre.core.events import DeliveryObservation
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.recovery.radio_observations import project_radio_packet_observations
from medre.core.rendering.radio_progress import RadioAttemptProgress
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioHandoffOrigin, RadioLocalHandoff


def _checkpoint(*, complete=True, inherited=False):
    """Build retained packet facts, optionally incomplete or inherited from attempt one."""
    provenance = DeliveryAttemptProvenance(
        "evt-1", "plan-1", "meshtastic", "0", "obox-1", 2, "retry"
    )
    plan = plan_radio_text("café α " * 15, max_text_bytes=35)
    progress = RadioAttemptProgress.bind(
        outbox_id="obox-1", attempt_number=2, plan=plan
    )
    count = len(plan.parts) if complete else 1
    for ordinal in range(1, count + 1):
        progress = progress.begin(ordinal).observe(
            ordinal,
            handoff_id=RadioLocalHandoff(
                ordinal,
                str(ordinal),
                "0",
                "local_transport",
                origin=(
                    RadioHandoffOrigin("obox-1", 1)
                    if inherited and ordinal == 1
                    else None
                ),
            ).encode(),
        )
    return StoredRadioAttempt(provenance, progress, count * 2)


def _ack(checkpoint, ordinal, **updates):
    """Construct one exactly scoped observation, with explicit mutations for negative cases."""
    fact = RadioLocalHandoff.from_part(
        checkpoint.progress.parts[ordinal - 1], checkpoint.provenance
    )
    origin = fact.origin
    assert origin is not None
    args = dict(
        observation_id=f"obs-{ordinal}",
        event_id="evt-1",
        delivery_plan_id="plan-1",
        target_adapter="meshtastic",
        target_channel="0",
        native_channel_id="0",
        outbox_id=origin.outbox_id,
        attempt_number=origin.attempt_number,
        adapter_message_id=fact.native_message_id,
        state="delivered",
        confirmation_level="remote_service",
        metadata={
            "radio_packet": {
                "ordinal": ordinal,
                "origin_outbox_id": origin.outbox_id,
                "origin_attempt_number": origin.attempt_number,
            }
        },
    )
    args.update(updates)
    return DeliveryObservation(**args)


def test_complete_native_ack_projection_is_read_only():
    """Complete packet coverage reports evidence without mutating the checkpoint."""
    cp = _checkpoint()
    ack = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    before = cp.progress
    summary = project_radio_packet_observations(cp, ack)
    assert summary.status == "all_packets_observed"
    assert summary.observation_floor == "remote_service"
    assert summary.native_submission_authorized is False
    assert cp.progress is before


def test_missing_packet_does_not_complete_parent():
    """One observed packet cannot stand in for an entire multipart plan."""
    cp = _checkpoint()
    summary = project_radio_packet_observations(cp, [_ack(cp, 1)])
    assert summary.status == "partial"
    assert summary.observed_parts == 1
    assert summary.observation_floor is None


def test_unaccepted_tail_remains_incomplete():
    """Remote evidence for a prefix cannot erase unsent local work."""
    cp = _checkpoint(complete=False)
    summary = project_radio_packet_observations(cp, [_ack(cp, 1)])
    assert summary.status == "partial"
    assert summary.reason == "local_handoff_incomplete"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("adapter_message_id", "stale", "partial"),
        ("native_channel_id", "other", "partial"),
        ("target_adapter", "meshcore", "partial"),
        ("event_id", "other", "partial"),
        ("attempt_number", 500, "partial"),
        ("state", "failed", "held"),
        ("confirmation_level", "local_transport", "partial"),
    ],
)
def test_inconsistent_or_weak_packet_does_not_complete(field, value, expected):
    """Unrelated facts remain partial while a matching negative report holds the plan."""
    cp = _checkpoint()
    obs = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    obs[0] = _ack(cp, 1, **{field: value})
    summary = project_radio_packet_observations(cp, obs)
    assert summary.status == expected


def test_negative_observation_blocks_conflicting_positive():
    """Positive evidence cannot hide an exactly correlated contradictory report."""
    cp = _checkpoint()
    obs = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    obs += [_ack(cp, 1, observation_id="failed-1", state="rejected")]
    assert project_radio_packet_observations(cp, obs).status == "held"


def test_inherited_origin_is_required_not_new_claim():
    """A continued packet retains the generation that actually accepted its handoff."""
    cp = _checkpoint(inherited=True)
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    assert (
        project_radio_packet_observations(cp, observations).status
        == "all_packets_observed"
    )

    spoofed = msgspec.structs.replace(observations[0], attempt_number=2)
    observations[0] = spoofed
    assert project_radio_packet_observations(cp, observations).status == "partial"


def test_observation_without_native_identity_does_not_qualify():
    """An absent native correlation handle cannot qualify a numbered packet ACK."""
    cp = _checkpoint()
    part = cp.progress.parts[0]

    cp = replace(
        cp,
        progress=replace(
            cp.progress,
            parts=(
                replace(
                    part,
                    handoff_id=RadioLocalHandoff(
                        1, None, "0", "local_transport"
                    ).encode(),
                ),
                *cp.progress.parts[1:],
            ),
        ),
    )
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    assert project_radio_packet_observations(cp, observations).status == "partial"


def test_duplicate_native_packet_ids_are_not_independent_evidence():
    """A reused transport identity cannot be counted as independent packet evidence."""
    cp = _checkpoint()
    first = cp.progress.parts[0]
    second = cp.progress.parts[1]
    ambiguous = RadioLocalHandoff(2, "1", "0", "local_transport").encode()
    cp = replace(
        cp,
        progress=replace(
            cp.progress,
            parts=(
                first,
                replace(second, handoff_id=ambiguous),
                *cp.progress.parts[2:],
            ),
        ),
    )
    summary = project_radio_packet_observations(cp, [])
    assert summary.status == "held"
    assert summary.reason == "ambiguous_native_packet_identity"


def test_reused_observation_identity_with_disagreeing_claim_is_held():
    """One immutable observation identity cannot describe two different packet facts."""
    cp = _checkpoint()
    positive = _ack(cp, 1)
    conflicting = _ack(cp, 2, observation_id=positive.observation_id)
    summary = project_radio_packet_observations(cp, [positive, conflicting])
    assert summary.status == "held"
    assert summary.reason == "conflicting_observation_identity"


@pytest.mark.parametrize(
    "origin", [RadioHandoffOrigin("other", 1), RadioHandoffOrigin("obox-1", 3)]
)
def test_unrelated_or_future_acceptance_origin_is_held(origin):
    """A retained part cannot borrow acceptance from another delivery or its future."""
    cp = _checkpoint()
    first = cp.progress.parts[0]
    fact = RadioLocalHandoff.from_part(first, cp.provenance)

    cp = replace(
        cp,
        progress=replace(
            cp.progress,
            parts=(
                replace(
                    first,
                    handoff_id=msgspec.structs.replace(fact, origin=origin).encode(),
                ),
                *cp.progress.parts[1:],
            ),
        ),
    )
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    assert project_radio_packet_observations(cp, observations).status == "held"


def test_boolean_attempt_number_cannot_qualify_packet_observation():
    """Python boolean equality must not coerce a callback into an accepting attempt."""

    cp = _checkpoint(inherited=True)
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    observations[0] = msgspec.structs.replace(observations[0], attempt_number=True)
    assert project_radio_packet_observations(cp, observations).status == "partial"


@pytest.mark.parametrize("state", ["submitting", "unknown"])
def test_uncertain_submission_is_held_without_correlating_callbacks(state):
    """An in-flight native boundary cannot become observation-complete evidence."""
    cp = _checkpoint(complete=False)
    progress = cp.progress.begin(2)
    if state == "unknown":
        progress = progress.observe(2)
    cp = StoredRadioAttempt(cp.provenance, progress, 3 if state == "submitting" else 4)
    summary = project_radio_packet_observations(cp, [_ack(cp, 1)])
    assert summary.status == "held"
    assert summary.reason == "uncertain_native_submission"


def test_generic_observation_does_not_count_as_a_packet_ack():
    """A parent-shaped transport report cannot stand in for numbered packet proof."""
    cp = _checkpoint()
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    observations[0] = _ack(cp, 1, metadata={})
    assert project_radio_packet_observations(cp, observations).status == "partial"


def test_observation_floor_is_the_weakest_packet_evidence():
    """One service-only ACK prevents an end-to-end whole-plan evidence claim."""
    cp = _checkpoint()
    observations = [
        _ack(cp, i, confirmation_level="end_to_end")
        for i in range(1, len(cp.progress.parts) + 1)
    ]
    observations[0] = _ack(cp, 1, confirmation_level="remote_service")
    summary = project_radio_packet_observations(cp, observations)
    assert summary.status == "all_packets_observed"
    assert summary.observation_floor == "remote_service"


def test_identical_observation_replay_is_idempotent():
    """Replaying an identical fact changes neither packet count nor held status."""
    cp = _checkpoint()
    observations = [_ack(cp, i) for i in range(1, len(cp.progress.parts) + 1)]
    assert project_radio_packet_observations(
        cp, observations + observations
    ) == project_radio_packet_observations(cp, observations)
