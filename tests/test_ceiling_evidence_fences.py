"""Adversarial evidence-version and delivery-identity fences for the ceiling."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Literal

import msgspec
import pytest

from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptCommitRejected,
)
from medre.core.events import DeliveryReceipt
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.evidence.handoff_resolution import (
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
)
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
from tests.helpers.handoff_resolution import (
    _WORKER,
    _bind_and_progress,
    _due_row,
    _observation,
    _plan,
    _provenance,
    _seed_reserved_claim,
)
from tests.helpers.storage_outbox import append_receipt_with_parent


def _receipt(
    receipt_id: str,
    provenance: DeliveryAttemptProvenance,
    *,
    attempt_number: int,
    status: Literal[
        "queued",
        "sent",
        "failed",
        "dead_lettered",
        "cancelled",
        "abandoned",
        "suppressed",
    ],
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        status=status,
        outbox_id=provenance.outbox_id,
        attempt_number=attempt_number,
        source="retry",
    )


async def test_abandonment_rejects_old_receipt_and_targets_the_stamped_attempt(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        _receipt(
            "old-abandoned",
            provenance,
            attempt_number=1,
            status="abandoned",
        ),
    )
    await append_receipt_with_parent(
        temp_storage,
        _receipt(
            "sent-attempt-2",
            provenance,
            attempt_number=2,
            status="sent",
        ),
    )
    await temp_storage._write(
        "UPDATE delivery_outbox SET attempt_number=1, active_attempt=2, "
        "status='in_progress' WHERE outbox_id=?",
        (stored.outbox_id,),
    )

    # The old abandoned receipt cannot exempt attempt 2, and the guard must
    # examine attempt 2's sent receipt rather than the pre-stamp attempt 1.
    assert not await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        receipt_id="old-abandoned",
        attempt_number=2,
        expected_receipt_count=1,
    )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"


async def test_abandonment_rejects_a_restored_packet_scope(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation(
            "packet-scope",
            provenance,
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": stored.outbox_id,
            },
        )
    )

    # The worker captured a zero-scope resolution, then the sweep restored
    # the legacy packet scope. The terminal write must refuse.
    assert not await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        attempt_number=1,
        expected_observation_count=1,
        expected_observation_scope_revision=0,
    )
    assert await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        attempt_number=1,
        expected_observation_count=1,
        expected_observation_scope_revision=1,
    )


async def test_resolver_reports_the_packet_scope_evidence_version(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation(
            "packet-scope-count",
            provenance,
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": stored.outbox_id,
            },
        )
    )
    resolution = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert resolution.observation_count == 1
    assert resolution.observation_scope_revision == 1


async def test_claiming_an_uncertain_retry_preserves_it_through_the_sweep(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=1,
        expected_worker_id=_WORKER,
        failure_kind_detail=UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    )
    claimed = await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert claimed.active_attempt is None

    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    typed = await temp_storage._read_one(
        "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
        "WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert typed == {"attempt_number": 1, "state": "uncertain_native_handoff"}
    projected = await temp_storage.get_outbox_item(stored.outbox_id)
    assert projected is not None
    assert projected.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL

    await temp_storage.release_outbox_claim(
        stored.outbox_id, _WORKER, release_status="retry_wait"
    )
    assert await temp_storage.append_delivery_observation(
        _observation("late-after-claim-release", provenance)
    )


async def test_attempt_zero_uncertain_retry_keeps_the_ordinary_retry_path(
    temp_storage: SQLiteStorage,
) -> None:
    stored, _provenance = await _seed_reserved_claim(temp_storage)
    await temp_storage._write(
        "UPDATE delivery_outbox SET attempt_number=0, active_attempt=NULL, "
        "status='in_progress', worker_id=?, lease_until=? WHERE outbox_id=?",
        (
            _WORKER,
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            stored.outbox_id,
        ),
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=0,
        expected_worker_id=_WORKER,
        failure_kind_detail=UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    )
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_handoff_uncertainty WHERE outbox_id=?",
            (stored.outbox_id,),
        )
        is None
    )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "retry_wait"


async def test_checkpoint_provenance_must_match_the_parent_outbox(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    forged = DeliveryAttemptProvenance(
        event_id="forged-event",
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        source=provenance.source,
        replay_run_id=provenance.replay_run_id,
    )
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET provenance_json=? WHERE outbox_id=?",
        (msgspec.json.encode(forged).decode(), stored.outbox_id),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)

    fact = await temp_storage._read_one(
        "SELECT 1 FROM radio_accepted_part_facts WHERE outbox_id=? "
        "AND attempt_number=1",
        (stored.outbox_id,),
    )
    assert fact is None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.parts[0].handoff_id == "{}"
    assert await temp_storage.append_delivery_observation(
        _observation("forged-provenance-message", provenance)
    )
    verdict = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert verdict.verdict == "unresolved"


async def test_native_completion_is_recorded_after_only_the_lease_expired(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    bound = await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    assert bound is not None
    started = await temp_storage.begin_radio_part(bound, _WORKER, 1)
    assert started is not None
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None
    await temp_storage._write(
        "UPDATE delivery_outbox SET lease_until=? WHERE outbox_id=?",
        (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            stored.outbox_id,
        ),
    )
    handoff = RadioLocalHandoff(1, "native-after-lease", "ch-0", "local_transport")
    finished = await temp_storage.observe_radio_part(
        started, _WORKER, 1, handoff_id=handoff.encode()
    )
    assert finished is not None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.parts[0].state == "accepted"


async def test_observation_only_abandonment_keeps_the_implicit_zero_receipt_guard(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        _receipt(
            "failed-attempt-1",
            provenance,
            attempt_number=1,
            status="failed",
        ),
    )
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().abandon_retry_outbox(
            temp_storage,
            item,
            error_summary="content_authority_denied",
            expected_observation_count=0,
        )


async def test_committed_sent_receipt_settles_without_replay(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    receipt = _receipt(
        "sent-current-attempt",
        provenance,
        attempt_number=1,
        status="sent",
    )
    await append_receipt_with_parent(temp_storage, receipt)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    finalization = await DeliveryLifecycleService().settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[receipt]
    )
    assert finalization.outcome == "accepted"
    assert finalization.receipt_id == "sent-current-attempt"
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.receipt_id == "sent-current-attempt"


async def test_late_ack_error_text_is_not_retained_after_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    observation = msgspec.structs.replace(
        _observation("late-error", provenance), error="late provider secret"
    )
    assert await temp_storage.append_delivery_observation(observation)
    diagnostic = await temp_storage._read_one(
        "SELECT 1 FROM delivery_evidence_diagnostics "
        "WHERE family='observation' AND row_id='late-error'"
    )
    assert diagnostic is None


async def test_reservation_rejects_a_restored_packet_scope(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=1,
        expected_worker_id=_WORKER,
        failure_kind_detail=UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    )
    claimed = await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert claimed.active_attempt is None
    assert await temp_storage.append_delivery_observation(
        _observation(
            "packet-scope-before-reservation",
            provenance,
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": stored.outbox_id,
            },
        )
    )
    assert (
        await temp_storage.reserve_outbox_attempt(
            stored.outbox_id,
            _WORKER,
            1,
            expected_observation_scope_revision=0,
        )
        is None
    )
    assert (
        await temp_storage.reserve_outbox_attempt(
            stored.outbox_id,
            _WORKER,
            1,
            expected_observation_scope_revision=1,
        )
        == 2
    )


async def test_fresh_abandonment_finalizes_only_the_effective_attempt(
    temp_storage: SQLiteStorage,
) -> None:
    """A fresh abandonment cannot stamp a newer attempt over acceptance.

    The acceptance fence checks the row's stored effective attempt, so a
    queued admission on the effective generation stops a caller stamping a
    newer attempt number over it.
    """
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        _receipt("queued-effective", provenance, attempt_number=1, status="queued"),
    )
    await temp_storage._write(
        "UPDATE delivery_outbox SET attempt_number=1, active_attempt=NULL, "
        "status='in_progress' WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert not await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        attempt_number=2,
    )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"


async def test_evidence_reader_prefers_the_summary_in_one_snapshot(
    temp_storage: SQLiteStorage,
) -> None:
    """The retained summary and the live checkpoint are observed together.

    A sweep committing between two separate reads could remove both
    representations and make preserved acceptance evidence look missing.
    One statement observes both; the retained summary wins when present,
    so a seeded summary is returned even beside a denied live checkpoint.
    """
    import json

    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted"])
    await _install_elapsed_policy(temp_storage, provenance.event_id)
    # Pre-sweep: the denied live checkpoint projects compact evidence.
    live = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert live is not None and live.revision >= 1
    # A retained summary beside the still-present checkpoint wins.
    await temp_storage._write(
        "INSERT INTO radio_attempt_evidence_summaries "
        "(outbox_id, attempt_number, provenance_json, parts_json, revision) "
        "VALUES (?, 1, ?, ?, 9) "
        "ON CONFLICT(outbox_id, attempt_number) DO UPDATE SET revision=9",
        (
            stored.outbox_id,
            json.dumps(
                {
                    "event_id": provenance.event_id,
                    "delivery_plan_id": provenance.delivery_plan_id,
                    "target_adapter": provenance.target_adapter,
                    "target_channel": provenance.target_channel,
                    "outbox_id": provenance.outbox_id,
                    "attempt_number": 1,
                    "source": "retry",
                    "replay_run_id": None,
                }
            ),
            json.dumps([{"ordinal": 1, "state": "accepted", "handoff_id": "origin"}]),
        ),
    )
    combined = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert combined is not None
    assert combined.revision == 9
    assert [part.state for part in combined.parts] == ["accepted"]


async def test_sweep_rejects_a_live_lane_claim_on_a_later_generation(
    temp_storage: SQLiteStorage,
) -> None:
    """No attempt after the first can legitimately carry the live lane.

    A live claim with a replay run cannot even decode, and a replay-run
    mismatch is rejected by identity, so the constructible source-only
    forgery is exactly this one: a later generation of a live-origin row
    rewriting its lane to ``live``. The sweep must keep it fail-closed.
    """
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=provenance.attempt_number,
        expected_worker_id=_WORKER,
    )
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    assert (
        await temp_storage.reserve_outbox_attempt(
            stored.outbox_id, _WORKER, provenance.attempt_number
        )
        == provenance.attempt_number + 1
    )
    retry_claim = _provenance(
        stored.outbox_id,
        provenance.attempt_number + 1,
        event_id=provenance.event_id,
        source="retry",
    )
    await _bind_and_progress(
        temp_storage, retry_claim, ["accepted", "unsent", "unsent"]
    )
    forged = replace(retry_claim, source="live")
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET provenance_json=? "
        "WHERE outbox_id=? AND attempt_number=?",
        (
            msgspec.json.encode(forged).decode(),
            stored.outbox_id,
            retry_claim.attempt_number,
        ),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM radio_accepted_part_facts WHERE outbox_id=?",
            (stored.outbox_id,),
        )
        is None
    )
    summary = await temp_storage.get_radio_attempt_evidence(
        stored.outbox_id, retry_claim.attempt_number
    )
    assert summary is not None and summary.parts[0].handoff_id == "{}"


async def test_sweep_keeps_an_older_live_lane_checkpoint_across_the_retry_transition(
    temp_storage: SQLiteStorage,
) -> None:
    """A live-lane attempt-1 checkpoint survives the row's live->retry move.

    ``reserve_outbox_attempt`` stamps the row ``retry`` for every later
    generation, so the sweep cannot treat an older attempt's original live
    lane as alien provenance: the reservation-time source is not
    reconstructable from the newer row (the resolver skips the same check
    for older attempts). The accepted handoff must keep its closed
    evidence and mint the original-acceptance facts the late packet-ACK
    path reconciles through.
    """
    stored, _seeded_provenance = await _seed_reserved_claim(temp_storage)
    live_claim = _provenance(
        stored.outbox_id,
        1,
        event_id=stored.event_id,
        source="live",
    )
    await _bind_and_progress(temp_storage, live_claim, ["accepted", "unsent", "unsent"])
    await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=1,
        expected_worker_id=_WORKER,
    )
    await _due_row(temp_storage, stored.outbox_id, _WORKER)
    # The guarded retry reservation is what moves the row live->retry.
    assert await temp_storage.reserve_outbox_attempt(stored.outbox_id, _WORKER, 1) == 2
    row = await temp_storage._read_one(
        "SELECT dispatch_source FROM delivery_outbox WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert row is not None and row["dispatch_source"] == "retry"
    now = await _install_elapsed_policy(temp_storage, stored.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    fact = await temp_storage._read_one(
        "SELECT native_message_id FROM radio_accepted_part_facts "
        "WHERE outbox_id=? AND attempt_number=1 AND ordinal=1",
        (stored.outbox_id,),
    )
    assert fact is not None and fact["native_message_id"] == "native-1"
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.provenance.source == "live"
    assert summary.parts[0].state == "accepted"
    assert summary.parts[0].handoff_id != "{}"


async def test_stale_checkpoint_cannot_mint_acceptance_behind_newer_summary(
    temp_storage: SQLiteStorage,
) -> None:
    """An old accepted checkpoint cannot modify a newer undecided attempt."""
    import json

    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    await temp_storage._write(
        "DELETE FROM radio_accepted_part_facts WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM radio_accepted_part_facts WHERE outbox_id=?",
            (stored.outbox_id,),
        )
        is None
    )
    await temp_storage._write(
        "INSERT INTO radio_attempt_evidence_summaries "
        "(outbox_id, attempt_number, provenance_json, parts_json, revision) "
        "VALUES (?, ?, ?, ?, 9)",
        (
            stored.outbox_id,
            provenance.attempt_number,
            msgspec.json.encode(provenance).decode(),
            json.dumps(
                [
                    {"ordinal": 1, "state": "submitting", "handoff_id": None},
                    {"ordinal": 2, "state": "unsent", "handoff_id": None},
                    {"ordinal": 3, "state": "unsent", "handoff_id": None},
                ],
                separators=(",", ":"),
            ),
        ),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None and summary.revision == 9
    assert summary.parts[0].state == "submitting"
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM radio_accepted_part_facts WHERE outbox_id=?",
            (stored.outbox_id,),
        )
        is None
    )
