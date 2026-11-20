"""Uncertain-native-handoff remediation: resolve-by-evidence contract tests.

Gap G02 remediation. When claim reconciliation reclaims a reserved dispatch
whose receipt evidence does not settle the attempt, a bounded, read-only
evidence pass decides one of three verdicts over what storage already holds:

* ``delivered_with_evidence`` — a fenced radio checkpoint shows the whole
  plan handed off, or adapter-reported observations prove provider-side
  acceptance of the whole message; the obligation settles ``sent`` through
  the existing guarded transition (no receipt is fabricated);
* ``definitively_not_submitted`` — the radio grant fence proves no native
  call was ever authorized; normal retry semantics follow with an
  evidence-backed diagnostic;
* ``unresolved`` — everything else; the attempt is consumed conservatively
  and the uncertainty is recorded with an operator-visible detail, to be
  reconsidered before a new reservation (bounded by the retry budget).

The decision is idempotent, race-safe under the claim fencing, and honest:
absence of evidence never proves rejection and conflicting evidence prevents
settlement on that pass. Normal retry may still dispatch after an unresolved
pre-dispatch recheck. Replay provenance is never rewritten by a resolution.
"""

from __future__ import annotations

import pytest

from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptCommitRejected,
)
from medre.core.events import DeliveryReceipt
from medre.core.evidence.handoff_resolution import (
    HANDOFF_NOT_SUBMITTED_DETAIL,
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
    UncertainHandoffResolution,
    resolve_uncertain_handoff,
)
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.rendering.radio_progress import RadioAttemptProgress, RadioPartProgress
from medre.core.storage.backend import StorageError
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.handoff_resolution import (
    _WORKER,
    _bind_and_progress,
    _checkpoint,
    _claimed_row,
    _due_row,
    _identity,
    _observation,
    _past,
    _plan,
    _provenance,
    _row,
    _seed_reserved_claim,
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


# ---------------------------------------------------------------------------
# Storage-backed resolution service tests
# ---------------------------------------------------------------------------


async def test_reconcile_settles_complete_handoff_as_sent(
    temp_storage: SQLiteStorage,
) -> None:
    """A reclaimed radio dispatch whose plan fully handed off settles sent."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "accepted"]
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "accepted"
    assert finalization.receipt_id is None
    assert finalization.attempt_number == 1
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    assert row.attempt_number == 1  # type: ignore[attr-defined]
    assert row.active_attempt is None  # type: ignore[attr-defined]
    # No receipt is fabricated; the checkpoint remains the evidence.
    assert await temp_storage.list_receipts_for_outbox(stored.outbox_id) == []
    checkpoint = await temp_storage.get_radio_attempt(stored.outbox_id, 1)
    assert checkpoint is not None
    assert checkpoint.progress.handoff_complete


async def test_reconcile_without_resolver_keeps_legacy_ambiguous_consumption(
    temp_storage: SQLiteStorage,
) -> None:
    """Without the resolver the merged ambiguous-dispatch rule is unchanged."""
    stored, _provenance_info = await _seed_reserved_claim(temp_storage)
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
    )

    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "retry_wait"  # type: ignore[attr-defined]
    assert row.failure_kind_detail == "dispatch_outcome_unknown"  # type: ignore[attr-defined]


@pytest.mark.parametrize("max_attempts", [1, 4])
async def test_receipt_less_attempt_rechecks_acceptance_appended_during_reclaim(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch, max_attempts: int
) -> None:
    """A stale empty read cannot consume even the final reserved attempt."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage, adapter="fake_presentation", channel=None
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")
    lifecycle = DeliveryLifecycleService()
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    policy = RetryPolicy(max_attempts=max_attempts, jitter=False)
    method = "mark_outbox_dead_lettered" if max_attempts == 1 else "mark_outbox_retry_wait"
    original = getattr(temp_storage, method)

    async def append_before_commit(*args, **kwargs):
        assert await temp_storage.append_delivery_observation(
            _observation("obs-late-acceptance", provenance, native_channel_id=None)
        )
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(temp_storage, method, append_before_commit)
        with pytest.raises(RetryAttemptCommitRejected):
            await lifecycle.reconcile_retry_claim(
                temp_storage, claimed, policy, handoff_resolver=resolver
            )

    row = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-b")
    assert row.active_attempt == provenance.attempt_number
    result = await lifecycle.reconcile_retry_claim(
        temp_storage, row, policy, handoff_resolver=resolver
    )
    assert result is not None and result.outcome == "accepted"


async def test_reconcile_settles_empty_grant_fence_with_not_submitted_detail(
    temp_storage: SQLiteStorage,
) -> None:
    """Evidence-backed non-submission consumes the attempt under retry semantics."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["unsent", "unsent"])
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "retry_wait"  # type: ignore[attr-defined]
    assert row.attempt_number == 1  # type: ignore[attr-defined]
    assert row.failure_kind_detail == HANDOFF_NOT_SUBMITTED_DETAIL  # type: ignore[attr-defined]


async def test_reconcile_records_uncertain_handoff_for_operator(
    temp_storage: SQLiteStorage,
) -> None:
    """An unresolved interrupted send stays explicitly, visibly uncertain."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unknown"])
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "retry_wait"  # type: ignore[attr-defined]
    assert row.attempt_number == 1  # type: ignore[attr-defined]
    assert row.active_attempt is None  # type: ignore[attr-defined]
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]
    assert row.error_summary is not None  # type: ignore[attr-defined]
    assert "uncertain" in row.error_summary.lower()  # type: ignore[attr-defined]


async def test_exhausted_budget_dead_letters_with_uncertain_detail(
    temp_storage: SQLiteStorage,
) -> None:
    """Budget exhaustion keeps the uncertainty detail on the terminal row."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unknown"])
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=1, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "dead_lettered"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "dead_lettered"  # type: ignore[attr-defined]
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]


async def test_provider_observation_settles_receipt_less_send_as_sent(
    temp_storage: SQLiteStorage,
) -> None:
    """A receipt-less non-radio attempt with recorded acceptance settles sent."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage, adapter="fake_presentation", channel=None
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")
    admitted = await temp_storage.append_delivery_observation(
        _observation("obs-late-acceptance", provenance, native_channel_id=None)
    )
    assert admitted is True

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "accepted"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    assert await temp_storage.list_receipts_for_outbox(stored.outbox_id) == []


async def test_resolution_is_idempotent_across_reclaims(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost settlement commit re-resolves to the same verdict and settles."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "accepted"]
    )
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    lifecycle = DeliveryLifecycleService()

    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    async def failing_sent(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("storage lost the settlement commit")

    monkeypatch.setattr(temp_storage, "mark_outbox_sent", failing_sent)
    with pytest.raises(RuntimeError):
        await lifecycle.reconcile_retry_claim(
            temp_storage,
            claimed,
            RetryPolicy(max_attempts=4, jitter=False),
            handoff_resolver=resolver,
        )
    monkeypatch.undo()
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "in_progress"  # type: ignore[attr-defined]

    # The lease expires; a restarted worker reclaims the same reservation and
    # re-runs resolution to the same verdict, which then commits.
    await temp_storage._write_rowcount(
        "UPDATE delivery_outbox SET lease_until = ? WHERE outbox_id = ?",
        (_past().isoformat(), stored.outbox_id),
    )
    reclaimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-b")
    second = await lifecycle.reconcile_retry_claim(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=resolver,
    )
    assert second is not None
    assert second.outcome == "accepted"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    assert row.attempt_number == 1  # type: ignore[attr-defined]


async def test_settlement_respects_stolen_claim(
    temp_storage: SQLiteStorage,
) -> None:
    """A worker that lost its claim cannot commit a resolution settlement."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "accepted"]
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    # Another worker steals the row by reclaiming its expired lease before
    # the first worker reconciles; the live reservation stays on the row.
    await temp_storage._write_rowcount(
        "UPDATE delivery_outbox SET lease_until = ? WHERE outbox_id = ?",
        (_past().isoformat(), stored.outbox_id),
    )
    stolen = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-b")
    assert stolen.worker_id == "reconciler-b"
    assert stolen.active_attempt == 1

    lifecycle = DeliveryLifecycleService()
    with pytest.raises(Exception) as refused:
        await lifecycle.reconcile_retry_claim(
            temp_storage,
            claimed,  # stale worker snapshot for reconciler-a
            RetryPolicy(max_attempts=4, jitter=False),
            handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
        )
    assert type(refused.value).__name__ == "RetryAttemptCommitRejected"

    # The thief's own reconciliation sees the same evidence and settles it.
    thief_finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        stolen,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert thief_finalization is not None
    assert thief_finalization.outcome == "accepted"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]


async def test_replay_origin_row_settles_without_provenance_rewrite(
    temp_storage: SQLiteStorage,
) -> None:
    """A replay-origin obligation settles on its own row, provenance intact."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage,
        adapter="fake_presentation",
        channel=None,
        replay_run_id="run-remediation",
    )
    admitted = await temp_storage.append_delivery_observation(
        _observation("obs-replay-acceptance", provenance, native_channel_id=None)
    )
    assert admitted is True
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert finalization is not None
    assert finalization.outcome == "accepted"

    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    # Replay origin and its run survive the retry reservation and settlement.
    assert row.dispatch_source == "replay"  # type: ignore[attr-defined]
    assert row.replay_run_id == "run-remediation"  # type: ignore[attr-defined]


async def test_unreadable_checkpoint_evidence_stays_conservative(
    temp_storage: SQLiteStorage,
) -> None:
    """Corrupt retained evidence never upgrades or re-labels the attempt."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unknown"])
    await temp_storage._write_rowcount(
        "UPDATE radio_attempt_progress SET parts_json = ? WHERE outbox_id = ?",
        ('{"ordinal": 999}', stored.outbox_id),
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Unreadable evidence and settlement hygiene
# ---------------------------------------------------------------------------


async def test_unreadable_observation_evidence_stays_conservative(
    temp_storage: SQLiteStorage,
) -> None:
    """Corrupt observation rows never raise out of reconciliation."""
    stored, provenance = await _seed_reserved_claim(
        temp_storage, adapter="fake_presentation", channel=None
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")
    admitted = await temp_storage.append_delivery_observation(
        _observation("obs-corrupt-metadata", provenance, native_channel_id=None)
    )
    assert admitted is True
    await temp_storage._write_rowcount(
        "UPDATE delivery_observations SET metadata = ? WHERE outbox_id = ?",
        ('{"radio_packet": {"ordinal": ', stored.outbox_id),
    )

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    # Unreadable evidence neither proves nor refutes: the attempt is consumed
    # conservatively instead of stranding the reservation on a decode error.
    assert finalization is not None
    assert finalization.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]

    assert "resolution_evidence_unreadable" in row.error_summary  # type: ignore[attr-defined]
    assert "no retained radio checkpoint" not in row.error_summary  # type: ignore[attr-defined]


async def test_evidence_settlement_clears_a_stale_receipt_pointer(
    temp_storage: SQLiteStorage,
) -> None:
    """A receipt-less evidence settlement leaves no stale receipt pointer."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "accepted"]
    )
    # A pointer left behind by earlier lineage handling before the reclaim.
    await temp_storage._write_rowcount(
        "UPDATE delivery_outbox SET receipt_id = ? WHERE outbox_id = ?",
        ("rcpt-stale-lineage", stored.outbox_id),
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    finalization = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )

    assert finalization is not None
    assert finalization.outcome == "accepted"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    assert row.receipt_id is None  # type: ignore[attr-defined]
    assert await temp_storage.list_receipts_for_outbox(stored.outbox_id) == []


# ---------------------------------------------------------------------------
# Bounded pre-dispatch evidence recheck
# ---------------------------------------------------------------------------


async def test_pre_dispatch_recheck_settles_newly_readable_complete_handoff(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence unreadable at consumption is reconsidered before redispatch."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "accepted"]
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise StorageError("checkpoint evidence temporarily unreadable")

    monkeypatch.setattr(temp_storage, "get_radio_attempt", unavailable)
    lifecycle = DeliveryLifecycleService()
    first = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert first is not None
    assert first.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]
    monkeypatch.undo()

    reclaimed = await _due_row(temp_storage, stored.outbox_id, "reconciler-b")
    final = await lifecycle.recheck_last_uncertain_handoff(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert final is not None
    assert final.outcome == "accepted"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "sent"  # type: ignore[attr-defined]
    assert row.attempt_number == 1  # type: ignore[attr-defined]
    assert row.active_attempt is None  # type: ignore[attr-defined]


async def test_pre_dispatch_recheck_records_proven_non_submission_without_dispatch(
    temp_storage: SQLiteStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recheck that proves the fence empty records it and dispatches later."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["unsent", "unsent"])
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise StorageError("checkpoint evidence temporarily unreadable")

    monkeypatch.setattr(temp_storage, "get_radio_attempt", unavailable)
    lifecycle = DeliveryLifecycleService()
    first = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert first is not None
    assert first.outcome == "retry_wait"
    row = await _row(temp_storage, stored.outbox_id)
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL  # type: ignore[attr-defined]
    monkeypatch.undo()

    reclaimed = await _due_row(temp_storage, stored.outbox_id, "reconciler-b")
    final = await lifecycle.recheck_last_uncertain_handoff(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert final is not None
    assert final.outcome == "retry_wait"
    assert final.attempt_number == 1
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "retry_wait"  # type: ignore[attr-defined]
    assert row.failure_kind_detail == HANDOFF_NOT_SUBMITTED_DETAIL  # type: ignore[attr-defined]

    # The recheck is bounded, not a durable hold: the recorded determination
    # clears the uncertain marker, so a fresh claim dispatches normally.
    following = await _due_row(temp_storage, stored.outbox_id, "reconciler-c")
    assert (
        await lifecycle.recheck_last_uncertain_handoff(
            temp_storage,
            following,
            RetryPolicy(max_attempts=4, jitter=False),
            handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
        )
        is None
    )


async def test_pre_dispatch_recheck_leaves_unresolved_evidence_for_dispatch(
    temp_storage: SQLiteStorage,
) -> None:
    """Unresolved evidence never holds the dispatch and never invokes transport."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unknown"])
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    first = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert first is not None
    assert first.outcome == "retry_wait"

    reclaimed = await _due_row(temp_storage, stored.outbox_id, "reconciler-b")
    final = await lifecycle.recheck_last_uncertain_handoff(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert isinstance(final, UncertainHandoffResolution)
    assert final.verdict == "unresolved"
    assert final.observation_count == 0
    row = await _row(temp_storage, stored.outbox_id)
    assert row.status == "in_progress"  # type: ignore[attr-defined]
    assert row.worker_id == "reconciler-b"  # type: ignore[attr-defined]


async def test_pre_dispatch_recheck_requires_the_uncertain_marker(
    temp_storage: SQLiteStorage,
) -> None:
    """Rows not consumed as explicitly uncertain dispatch without a recheck."""
    stored, _provenance_info = await _seed_reserved_claim(temp_storage)
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reconciler-a")

    lifecycle = DeliveryLifecycleService()
    first = await lifecycle.reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4, jitter=False),
    )
    assert first is not None
    assert first.outcome == "retry_wait"

    reclaimed = await _due_row(temp_storage, stored.outbox_id, "reconciler-b")
    final = await lifecycle.recheck_last_uncertain_handoff(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4, jitter=False),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert final is None


async def test_reconcile_ignores_corrupt_observations_from_older_attempts(
    temp_storage: SQLiteStorage,
) -> None:
    stored, first = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation("obs-corrupt-older", first)
    )
    await temp_storage._write_rowcount(
        "UPDATE delivery_observations SET metadata = '{' WHERE observation_id = ?",
        ("obs-corrupt-older",),
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        _past().isoformat(),
        attempt_number=1,
        expected_worker_id=_WORKER,
    )
    claimed = await _due_row(temp_storage, stored.outbox_id, "second-attempt")
    assert (
        await temp_storage.reserve_outbox_attempt(
            claimed.outbox_id, "second-attempt", 1
        )
        == 2
    )
    assert await temp_storage.append_delivery_observation(
        _observation("obs-current", _provenance(stored.outbox_id, 2))
    )

    reclaimed = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    outcome = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        reclaimed,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert outcome is not None and outcome.outcome == "accepted"


@pytest.mark.parametrize("empty_fence", [False, True])
async def test_new_observation_prevents_settlement_from_a_stale_verdict(
    temp_storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch, empty_fence: bool
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    if empty_fence:
        await _bind_and_progress(temp_storage, provenance, ["unsent"] * 3)
        original = temp_storage.mark_outbox_retry_wait
        method = "mark_outbox_retry_wait"
        late = _observation("obs-late-accepted", provenance)
    else:
        assert await temp_storage.append_delivery_observation(
            _observation("obs-first-accepted", provenance)
        )
        original = temp_storage.mark_outbox_sent
        method = "mark_outbox_sent"
        late = _observation("obs-late-failed", provenance, state="failed")

    async def append_before_commit(*args, **kwargs):
        assert await temp_storage.append_delivery_observation(late)
        return await original(*args, **kwargs)

    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    lifecycle = DeliveryLifecycleService()
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    policy = RetryPolicy(max_attempts=4)
    with monkeypatch.context() as patch:
        patch.setattr(temp_storage, method, append_before_commit)
        with pytest.raises(RetryAttemptCommitRejected):
            await lifecycle.reconcile_retry_claim(
                temp_storage, claimed, policy, handoff_resolver=resolver
            )

    reclaimed = await _claimed_row(temp_storage, stored.outbox_id, "another-reclaimer")
    outcome = await lifecycle.reconcile_retry_claim(
        temp_storage, reclaimed, policy, handoff_resolver=resolver
    )
    assert outcome is not None and outcome.outcome == "retry_wait"
    settled = await temp_storage.get_outbox_item(stored.outbox_id)
    assert settled is not None
    assert settled.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL


@pytest.mark.parametrize("replay_channel,expected_failures", [("ch-0", 0), ("ch-1", 1)])
async def test_recovery_query_observes_receiptless_exact_settlement(
    temp_storage: SQLiteStorage, replay_channel: str, expected_failures: int
) -> None:
    original, provenance = await _seed_reserved_claim(temp_storage)
    receipt = DeliveryReceipt(
        receipt_id="rcpt-first-failure",
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        status="dead_lettered",
        failure_kind="adapter_permanent",
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
    )
    await temp_storage.append_receipt(receipt)
    assert await temp_storage.mark_outbox_dead_lettered(
        original.outbox_id,
        receipt_id=receipt.receipt_id,
        attempt_number=1,
        expected_worker_id=_WORKER,
    )
    assert len((await temp_storage.query_unresolved_deliveries()).items) == 1

    replay, later = await _seed_reserved_claim(
        temp_storage,
        outbox_id="obox-replay-query",
        replay_run_id="run-query",
        channel=replay_channel,
        attempt_number=1,
    )
    assert later.attempt_number > provenance.attempt_number
    assert await temp_storage.append_delivery_observation(
        _observation("obs-replay-sent", later)
    )
    claimed = await _claimed_row(temp_storage, replay.outbox_id, "reclaimer")
    result = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert (
        result is not None
        and result.outcome == "accepted"
        and result.receipt_id is None
    )
    assert (
        len((await temp_storage.query_unresolved_deliveries()).items)
        == expected_failures
    )
