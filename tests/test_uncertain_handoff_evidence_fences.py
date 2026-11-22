"""Exact-attempt evidence, reservation, and historical receipt ordering fences."""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from msgspec.structs import force_setattr, replace

from medre.core.delivery_authority import dispatch_source_matches_attempt
from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptCommitRejected,
    RetryAttemptFinalization,
)
from medre.core.events import DeliveryReceipt
from medre.core.evidence.handoff_resolution import (
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
    UncertainHandoffResolution,
    resolve_uncertain_handoff,
)
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _install_elapsed_policy
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
    _seed_reserved_claim,
)
from tests.helpers.storage_outbox import append_receipt_with_parent


@pytest.mark.parametrize("boundary", ["unknown", "submitting"])
@pytest.mark.parametrize("negative", [None, "failed", "rejected", "cancelled"])
def test_whole_message_acceptance_covers_granted_uncertain_last_part(
    boundary, negative
):
    provenance = _provenance("obox-last-grant", 1)
    checkpoint = _checkpoint(provenance, ["accepted", "accepted", boundary])
    observations = [_observation("whole-message", provenance)]
    if negative is not None:
        observations.append(_observation("negative", provenance, state=negative))
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=checkpoint,
        observations=observations,
    )
    assert resolution.verdict == (
        "delivered_with_evidence" if negative is None else "unresolved"
    )
    assert resolution.uncertain_parts == 1
    assert checkpoint.progress.parts[-1].state == boundary


@pytest.mark.parametrize("with_checkpoint", [False, True])
@pytest.mark.parametrize("contradiction", ["channel", "metadata"])
def test_exact_attempt_contradiction_cannot_be_discarded(
    with_checkpoint, contradiction
):
    provenance = _provenance("obox-exact-conflict", 1)
    good = _observation("accepted", provenance)
    bad = _observation("contradiction", provenance)
    if contradiction == "channel":
        bad = replace(bad, target_channel="wrong-channel")
    else:
        # Bypass construction normalization to model an unreadable record.
        force_setattr(bad, "metadata", [])
    verdict = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=(
            _checkpoint(provenance, ["accepted"] * 3) if with_checkpoint else None
        ),
        observations=[good, bad],
    )
    assert verdict.verdict == "unresolved"
    assert verdict.reasons == (
        (
            "observation_identity_mismatch"
            if contradiction == "channel"
            else "unreadable_packet_evidence"
        ),
    )


def test_message_acceptance_cannot_override_ungranted_part():
    provenance = _provenance("obox-ungranted", 1)
    resolution = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=_identity(),
        checkpoint=_checkpoint(provenance, ["accepted", "unknown", "unsent"]),
        observations=[_observation("whole-message", provenance)],
    )
    assert resolution.verdict == "unresolved"
    assert resolution.reasons == ("provider_acceptance_conflicts_with_unsent_parts",)


@pytest.mark.parametrize("origin", ["live", "replay", "retry"])
@pytest.mark.parametrize("lane", ["live", "replay", "retry"])
@pytest.mark.parametrize("attempt", [1, 2])
def test_source_coherence_accepts_only_documented_directions(origin, lane, attempt):
    assert dispatch_source_matches_attempt(origin, lane, attempt) == (
        origin == lane
        or (origin, lane) == ("replay", "retry")
        or ((origin, lane) == ("live", "retry") and attempt == 1)
    )


@pytest.mark.parametrize("states", [["accepted"] * 3, ["unsent"] * 3])
async def test_first_retry_worker_dispatch_binds_and_reconciles(temp_storage, states):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.dispatch_source == "live"
    assert provenance.source == "retry" and provenance.attempt_number == 1
    await _bind_and_progress(temp_storage, provenance, states)
    claim = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    verdict = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claim,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert verdict is not None
    assert verdict.outcome == ("accepted" if states[0] == "accepted" else "retry_wait")


async def test_retired_sender_cannot_mutate_empty_grant_fence(temp_storage):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    checkpoint = await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    assert checkpoint is not None
    claim = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    assert await temp_storage.begin_radio_part(checkpoint, _WORKER, 1) is None
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) == checkpoint
    result = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claim,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert result is not None and result.outcome == "retry_wait"


@pytest.mark.parametrize("late_acceptance", [False, True])
async def test_reservation_is_fenced_to_last_recheck_count(
    temp_storage, late_acceptance
):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    service = DeliveryLifecycleService()
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    policy = RetryPolicy(max_attempts=4)
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "first")
    await service.reconcile_retry_claim(
        temp_storage, claimed, policy, handoff_resolver=resolver
    )
    claimed = await _due_row(temp_storage, stored.outbox_id, "second")
    resolution = await service.recheck_last_uncertain_handoff(
        temp_storage, claimed, policy, handoff_resolver=resolver
    )
    assert isinstance(resolution, UncertainHandoffResolution)
    assert resolution.observation_count == 0
    if late_acceptance:
        assert await temp_storage.append_delivery_observation(
            _observation("late-acceptance", provenance)
        )
    reserved = await service.reserve_retry_attempt(
        temp_storage, claimed, expected_observation_count=resolution.observation_count
    )
    assert reserved == (None if late_acceptance else 2)
    if late_acceptance:
        row = await temp_storage.get_outbox_item(stored.outbox_id)
        assert row is not None and row.active_attempt is None
        assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL
        reclaimed = await _claimed_row(temp_storage, stored.outbox_id, "third")
        result = await service.recheck_last_uncertain_handoff(
            temp_storage, reclaimed, policy, handoff_resolver=resolver
        )
        assert result is not None and not isinstance(result, UncertainHandoffResolution)
        assert result.outcome == "accepted"


async def test_old_receipt_and_operational_deferral_do_not_erase_uncertainty(
    temp_storage,
):
    stored, first = await _seed_reserved_claim(temp_storage)
    receipt = DeliveryReceipt(
        receipt_id="previous-attempt-failure",
        event_id=first.event_id,
        delivery_plan_id=first.delivery_plan_id,
        target_adapter=first.target_adapter,
        target_channel=first.target_channel,
        status="failed",
        failure_kind="adapter_transient",
        outbox_id=first.outbox_id,
        attempt_number=1,
        source="retry",
    )
    await temp_storage.append_receipt(receipt)
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        _past().isoformat(),
        receipt_id=receipt.receipt_id,
        attempt_number=1,
        expected_worker_id=_WORKER,
    )
    row = await _due_row(temp_storage, stored.outbox_id, "second")
    assert await temp_storage.reserve_outbox_attempt(row.outbox_id, "second", 1) == 2
    row = await _claimed_row(temp_storage, row.outbox_id, "consume-second")
    service = DeliveryLifecycleService()
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    policy = RetryPolicy(max_attempts=4)
    await service.reconcile_retry_claim(
        temp_storage, row, policy, handoff_resolver=resolver
    )
    row = await _due_row(temp_storage, row.outbox_id, "defer-second")
    assert row.receipt_id == receipt.receipt_id and row.attempt_number == 2
    result = await service.recheck_last_uncertain_handoff(
        temp_storage, row, policy, handoff_resolver=resolver
    )
    assert isinstance(result, UncertainHandoffResolution)
    await service.defer_retry_outbox(
        temp_storage, row, policy, failure_kind="adapter_transient", attempt_number=2
    )
    row = await _due_row(temp_storage, row.outbox_id, "recover-second")
    assert row.attempt_number == 2
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL
    assert await temp_storage.append_delivery_observation(
        _observation("second-accepted", _provenance(row.outbox_id, 2, source="retry"))
    )
    result = await service.recheck_last_uncertain_handoff(
        temp_storage, row, policy, handoff_resolver=resolver
    )
    assert result is not None and not isinstance(result, UncertainHandoffResolution)
    assert result.outcome == "accepted"
    settled = await temp_storage.get_outbox_item(row.outbox_id)
    assert settled is not None and settled.receipt_id is None
    assert settled.attempt_number == 2


async def test_outboxless_failures_use_append_order_not_attempt_numbers(temp_storage):
    identity = _identity()

    async def fail(receipt_id, attempt_number, channel):
        await append_receipt_with_parent(
            temp_storage,
            DeliveryReceipt(
                receipt_id=receipt_id,
                event_id=identity.event_id,
                delivery_plan_id=identity.delivery_plan_id,
                target_adapter=identity.target_adapter,
                target_channel=channel,
                status="failed",
                failure_kind="adapter_permanent",
                attempt_number=attempt_number,
            ),
        )

    await fail("old-same-channel", 4, identity.target_channel)
    await fail("old-sibling", 4, "sibling")
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert provenance.attempt_number == 1
    assert await temp_storage.append_delivery_observation(
        _observation("accepted", provenance)
    )
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    final = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert final is not None and final.outcome == "accepted"
    page = await temp_storage.query_unresolved_deliveries()
    assert {row.receipt_id for row in page.items} == {"old-sibling"}
    await fail("new-same-channel", 1, identity.target_channel)
    page = await temp_storage.query_unresolved_deliveries()
    assert {row.receipt_id for row in page.items} == {"old-sibling", "new-same-channel"}


async def test_receipt_watermark_table_is_safe_additive_upgrade(tmp_path):
    path = str(tmp_path / "handoff-upgrade.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    try:
        stored, _ = await _seed_reserved_claim(storage)
        await storage._write_rowcount("DROP TABLE delivery_handoff_settlements", ())
    finally:
        await storage.close()
    upgraded = SQLiteStorage(path)
    await upgraded.initialize()
    try:
        assert await upgraded.get_outbox_item(stored.outbox_id) is not None
        assert (
            await upgraded._read_all("SELECT * FROM delivery_handoff_settlements") == []
        )
        prior = DeliveryReceipt(
            receipt_id="pre-upgrade-failure",
            event_id=stored.event_id,
            delivery_plan_id=stored.delivery_plan_id,
            target_adapter=stored.target_adapter,
            target_channel=stored.target_channel,
            status="failed",
            failure_kind="adapter_permanent",
            attempt_number=4,
        )
        await append_receipt_with_parent(upgraded, prior)
        provenance = _provenance(stored.outbox_id, 1, source="retry")
        assert await upgraded.append_delivery_observation(
            _observation("post-upgrade-acceptance", provenance)
        )
        claimed = await _claimed_row(upgraded, stored.outbox_id, "post-upgrade")
        result = await DeliveryLifecycleService().reconcile_retry_claim(
            upgraded,
            claimed,
            RetryPolicy(max_attempts=4),
            handoff_resolver=EvidenceBackedHandoffResolver(upgraded),
        )
        assert result is not None and result.outcome == "accepted"
        watermarks = await upgraded._read_all(
            "SELECT * FROM delivery_handoff_settlements"
        )
        assert len(watermarks) == 1
        assert (await upgraded.query_unresolved_deliveries()).items == []
    finally:
        await upgraded.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (
            await reopened._read_all("SELECT * FROM delivery_handoff_settlements")
            == watermarks
        )
        assert (await reopened.query_unresolved_deliveries()).items == []
    finally:
        await reopened.close()


async def test_empty_observation_metadata_is_not_acceptance(temp_storage):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation("unreadable-metadata", provenance)
    )
    await temp_storage._write_rowcount(
        "UPDATE delivery_observations SET metadata=? WHERE outbox_id=?",
        ("", stored.outbox_id),
    )
    item = await _claimed_row(temp_storage, stored.outbox_id, "reader")
    verdict = await EvidenceBackedHandoffResolver(temp_storage).resolve(item, 1)
    assert verdict.verdict == "unresolved"
    assert verdict.observation_count is None
    assert verdict.reasons == ("resolution_evidence_unreadable",)
    with pytest.raises(StorageError, match="evidence revision"):
        await DeliveryLifecycleService().reconcile_retry_claim(
            temp_storage,
            item,
            RetryPolicy(max_attempts=1),
            handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
        )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"


@pytest.mark.parametrize("metadata", ["", None])
def test_metadata_decoder_rejects_unreadable_empty_value(metadata) -> None:
    from medre.core.storage.sqlite.serde import _row_to_delivery_observation

    with pytest.raises(ValueError, match="unreadable"):
        _row_to_delivery_observation({"metadata": metadata})


def test_incomplete_delivery_identity_fails_closed() -> None:
    from medre.core.delivery_authority import DeliveryIdentity

    provenance = _provenance("obox-incomplete-identity", 1)
    identity = DeliveryIdentity("evt-handoff-resolution", "", "radio", "ch-0")
    verdict = resolve_uncertain_handoff(
        outbox_id=provenance.outbox_id,
        attempt_number=1,
        identity=identity,
        checkpoint=None,
        observations=[_observation("accepted", provenance)],
    )
    assert verdict.verdict == "unresolved"
    assert verdict.reasons == ("incomplete_delivery_identity",)


async def test_content_denied_abandonment_fences_new_evidence(temp_storage) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    item = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    assert await temp_storage.append_delivery_observation(
        _observation("late-acceptance-before-abandon", provenance)
    )
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().abandon_retry_outbox(
            temp_storage,
            item,
            error_summary="content_authority_denied",
            expected_observation_count=0,
            expected_checkpoint_revision=-1,
        )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None and row.status == "in_progress"


async def test_terminal_abandoned_receipt_records_despite_later_acceptance(
    temp_storage,
) -> None:
    """Recording an explicit abandoned terminal commits even beside acceptance.

    A fresh abandonment decision must never discard an observed acceptance,
    but committing a terminal receipt that already records abandonment is
    durable evidence, not a decision — the transition is exempt from the
    acceptance fence. Without a terminal receipt pointer the same
    abandonment is still refused.
    """
    stored, provenance = await _seed_reserved_claim(temp_storage)
    item = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    await append_receipt_with_parent(
        temp_storage,
        DeliveryReceipt(
            receipt_id="late-sent-receipt",
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            status="sent",
            outbox_id=provenance.outbox_id,
            attempt_number=1,
            source="retry",
        ),
    )
    await append_receipt_with_parent(
        temp_storage,
        DeliveryReceipt(
            receipt_id="explicit-abandoned-receipt",
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            status="abandoned",
            outbox_id=provenance.outbox_id,
            attempt_number=1,
            source="retry",
        ),
    )
    assert not await temp_storage.mark_outbox_abandoned(
        item.outbox_id,
        attempt_number=1,
        expected_worker_id=item.worker_id,
    )
    assert await temp_storage.mark_outbox_abandoned(
        item.outbox_id,
        receipt_id="explicit-abandoned-receipt",
        attempt_number=1,
        expected_worker_id=item.worker_id,
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None and row.status == "abandoned"
    assert row.receipt_id == "explicit-abandoned-receipt"


async def test_receipt_fence_rejects_terminal_transition_after_late_append(
    temp_storage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    item = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    await append_receipt_with_parent(
        temp_storage,
        DeliveryReceipt(
            receipt_id="late-sent-receipt",
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            status="sent",
            outbox_id=provenance.outbox_id,
            attempt_number=1,
            source="retry",
        ),
    )
    assert not await temp_storage.mark_outbox_dead_lettered(
        item.outbox_id,
        attempt_number=1,
        expected_worker_id=item.worker_id,
        expected_observation_count=0,
        expected_receipt_count=0,
        expected_checkpoint_revision=-1,
    )
    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None and row.status == "in_progress"


async def test_reservation_fences_the_checkpoint_revision_seen_at_verdict(
    temp_storage,
) -> None:
    """An existing checkpoint's revision is pinned from verdict to reservation."""
    stored, _ = await _seed_reserved_claim(temp_storage)
    # A live first dispatch owns its checkpoint directly, so the sender can
    # still advance it while a re-claimed row is awaiting its reservation.
    provenance = _provenance(stored.outbox_id, 1, source="live")
    await _bind_and_progress(
        temp_storage, provenance, ["accepted", "accepted", "submitting"]
    )
    service = DeliveryLifecycleService()
    resolver = EvidenceBackedHandoffResolver(temp_storage)
    policy = RetryPolicy(max_attempts=4)
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "first-reader")
    consumed = await service.reconcile_retry_claim(
        temp_storage, claimed, policy, handoff_resolver=resolver
    )
    assert consumed is not None and consumed.outcome == "retry_wait"
    due = await _due_row(temp_storage, stored.outbox_id, "second-reader")
    verdict = await service.recheck_last_uncertain_handoff(
        temp_storage, due, policy, handoff_resolver=resolver
    )
    assert isinstance(verdict, UncertainHandoffResolution)
    assert verdict.observation_count == 0
    checkpoint = await temp_storage.get_radio_attempt(stored.outbox_id, 1)
    assert checkpoint is not None
    stale_revision = verdict.checkpoint_revision
    assert stale_revision == checkpoint.revision and stale_revision >= 0

    # The submitting packet advances its checkpoint after the verdict but
    # before the guarded reservation write.
    advanced = await temp_storage.observe_radio_part(checkpoint, due.worker_id, 3)
    assert advanced is not None
    assert advanced.revision == stale_revision + 1

    reserved = await service.reserve_retry_attempt(
        temp_storage,
        due,
        expected_observation_count=verdict.observation_count,
        expected_receipt_count=0,
        expected_checkpoint_revision=stale_revision,
    )
    assert reserved is None
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"
    assert row.active_attempt is None

    # A fresh read resolves the advanced revision and the reservation commits.
    reclaimed = await _claimed_row(temp_storage, stored.outbox_id, "third-reader")
    fresh = await service.recheck_last_uncertain_handoff(
        temp_storage, reclaimed, policy, handoff_resolver=resolver
    )
    assert isinstance(fresh, UncertainHandoffResolution)
    assert fresh.checkpoint_revision == stale_revision + 1
    reserved = await service.reserve_retry_attempt(
        temp_storage,
        reclaimed,
        expected_observation_count=fresh.observation_count,
        expected_receipt_count=0,
        expected_checkpoint_revision=fresh.checkpoint_revision,
    )
    assert reserved == provenance.attempt_number + 1
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.active_attempt == reserved


async def test_recheck_uses_historical_checkpoint_source_after_newer_reservation(
    temp_storage,
) -> None:
    stored, _ = await _seed_reserved_claim(temp_storage)
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None
    old_source = _provenance(stored.outbox_id, 1, source="live")
    checkpoint = _checkpoint(old_source, ["accepted"] * 3)
    current = dataclass_replace(
        row, attempt_number=2, active_attempt=3, dispatch_source="retry"
    )
    provider = SimpleNamespace(
        get_radio_attempt=AsyncMock(return_value=checkpoint),
        read_handoff_observation_snapshot=AsyncMock(return_value=([], 0)),
    )
    resolved = await EvidenceBackedHandoffResolver(provider).resolve(current, 1)
    assert resolved.verdict == "delivered_with_evidence"
    assert resolved.checkpoint_revision == checkpoint.revision
    provider.get_radio_attempt.assert_awaited_once_with(stored.outbox_id, 1)
    provider.read_handoff_observation_snapshot.assert_awaited_once_with(
        stored.outbox_id, 1
    )


async def test_uncertain_backoff_accepts_exact_finalized_observation(temp_storage):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    claimed = await _claimed_row(temp_storage, stored.outbox_id, "first-reader")
    first = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert first is not None and first.outcome == "retry_wait"
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.active_attempt is None
    assert row.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL
    assert await temp_storage.append_delivery_observation(
        _observation("late-during-backoff", provenance)
    )
    later = _provenance(stored.outbox_id, provenance.attempt_number + 1)
    assert not await temp_storage.append_delivery_observation(
        _observation("wrong-later-attempt", later)
    )
    due = await _due_row(temp_storage, stored.outbox_id, "second-reader")
    final = await DeliveryLifecycleService().recheck_last_uncertain_handoff(
        temp_storage,
        due,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert final is not None and final.outcome == "accepted"
    settled = await temp_storage.get_outbox_item(stored.outbox_id)
    assert settled is not None and settled.status == "sent"


async def _install_elapsed_ceiling_for_handoff(storage, event_id):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    await storage._write(
        "INSERT INTO event_content_access_policies "
        "(event_id, version, hard_ceiling, allowed_consumers, "
        "max_optional_hold_microseconds, created_at) "
        "VALUES (?, 1, ?, '[]', 1000000, ?)",
        (event_id, (now - timedelta(seconds=1)).isoformat(), now.isoformat()),
    )
    return now


async def test_ceiling_preserves_uncertain_attempt_admission_without_diagnostic(
    temp_storage,
):
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
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    remaining = await temp_storage.get_outbox_item(stored.outbox_id)
    assert remaining is not None
    assert remaining.failure_kind_detail == UNCERTAIN_NATIVE_HANDOFF_DETAIL
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_evidence_diagnostics WHERE family='outbox' "
            "AND row_id=?",
            (stored.outbox_id,),
        )
        is None
    )
    assert await temp_storage.append_delivery_observation(
        _observation("late-after-ceiling", provenance)
    )
    observed = await temp_storage.list_delivery_observations_for_attempt(
        stored.outbox_id, 1
    )
    assert len(observed) == 1 and observed[0].metadata == {}
    assert observed[0].confirmation_level == "end_to_end"
    assert await temp_storage.append_delivery_observation(
        _observation(
            "late-malformed-scope",
            provenance,
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": "unverified provider text",
            },
        )
    )
    scope = await temp_storage._read_one(
        "SELECT scope_json FROM delivery_observation_scopes "
        "WHERE observation_id='late-malformed-scope'"
    )
    assert scope == {"scope_json": '{"radio_packet":null}'}
    assert await temp_storage.mark_outbox_cancelled(stored.outbox_id)
    assert not await temp_storage.append_delivery_observation(
        _observation("late-after-cancel", provenance)
    )


async def test_ceiling_keeps_accepted_packet_scope_but_erases_wire_plan(temp_storage):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None
    fact = await temp_storage._read_one(
        "SELECT native_message_id FROM radio_accepted_part_facts "
        "WHERE outbox_id=? AND attempt_number=1 AND ordinal=1",
        (stored.outbox_id,),
    )
    assert fact is not None and fact["native_message_id"] == "native-1"
    ack = _observation(
        "late-radio-packet",
        provenance,
        radio_packet={
            "ordinal": 1,
            "origin_attempt_number": 1,
            "origin_outbox_id": stored.outbox_id,
            "provider_secret": "sensitive",
        },
    )
    assert await temp_storage.append_radio_packet_observation(ack)
    assert not await temp_storage.append_radio_packet_observation(
        _observation(
            "false-packet",
            provenance,
            adapter_message_id="wrong",
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": stored.outbox_id,
            },
        )
    )
    read = await temp_storage.list_delivery_observations_for_attempt(
        stored.outbox_id, 1
    )
    assert len(read) == 1
    assert read[0].metadata == {
        "radio_packet": {
            "ordinal": 1,
            "origin_attempt_number": 1,
            "origin_outbox_id": stored.outbox_id,
        }
    }
    assert "sensitive" not in repr(read)
    raw = await temp_storage._read_one(
        "SELECT metadata FROM delivery_observations WHERE observation_id='late-radio-packet'"
    )
    assert raw is not None and raw["metadata"] == "{}"
    resolved = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert resolved.verdict == "unresolved"


async def test_complete_radio_handoff_still_settles_after_wire_plan_erasure(
    temp_storage,
):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    fingerprint = "fa" * 32
    await _bind_and_progress(
        temp_storage,
        provenance,
        ["accepted", "accepted", "accepted"],
        payload_fingerprint=fingerprint,
    )
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert all(part.state == "accepted" for part in summary.parts)
    assert all(
        "payload_fingerprint" not in (part.handoff_id or "") for part in summary.parts
    )
    retained = await temp_storage._read_one(
        "SELECT parts_json, provenance_json FROM radio_attempt_evidence_summaries "
        "WHERE outbox_id=? AND attempt_number=1",
        (stored.outbox_id,),
    )
    assert retained is not None
    assert fingerprint not in repr(retained)
    # The retained projection is identity-only: no wire text from the plan.
    assert "café α" not in repr(retained)

    claimed = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    verdict = await DeliveryLifecycleService().reconcile_retry_claim(
        temp_storage,
        claimed,
        RetryPolicy(max_attempts=4),
        handoff_resolver=EvidenceBackedHandoffResolver(temp_storage),
    )
    assert verdict is not None and verdict.outcome == "accepted"
    settled = await temp_storage.get_outbox_item(stored.outbox_id)
    assert settled is not None and settled.status == "sent"
    assert await temp_storage.list_receipts_for_outbox(stored.outbox_id) == []


async def test_expired_attempt_cannot_rebind_erased_wire_plan(temp_storage):
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted", "unsent", "unsent"])
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan()) is None
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None


async def test_staged_abandonment_cannot_consume_a_newer_active_attempt(temp_storage):
    stored, _ = await _seed_reserved_claim(temp_storage)
    active = await temp_storage.get_outbox_item(stored.outbox_id)
    assert active is not None and active.active_attempt == 1
    stale = dataclass_replace(active, active_attempt=None, attempt_number=0)
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().abandon_retry_outbox(
            temp_storage,
            stale,
            error_summary="content_authority_denied",
            expected_observation_count=0,
            expected_receipt_count=0,
        )
    assert (await temp_storage.get_outbox_item(stored.outbox_id)) == active


async def test_in_flight_native_completion_survives_wire_plan_erasure(temp_storage):
    """An already-granted call can finish, but erasure cannot start another call."""
    from medre.core.storage.radio_handoff import RadioLocalHandoff

    stored, provenance = await _seed_reserved_claim(temp_storage)
    bound = await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    assert bound is not None
    started = await temp_storage.begin_radio_part(bound, _WORKER, 1)
    assert started is not None
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None
    handoff = RadioLocalHandoff(1, "native-after-erasure", "ch-0", "local_transport")
    finished = await temp_storage.observe_radio_part(
        started, _WORKER, 1, handoff_id=handoff.encode()
    )
    assert finished is not None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.parts[0].state == "accepted"
    assert summary.revision == 2
    assert await temp_storage.begin_radio_part(finished, _WORKER, 2) is None
    assert await temp_storage.append_radio_packet_observation(
        _observation(
            "packet-after-erasure",
            provenance,
            adapter_message_id="native-after-erasure",
            radio_packet={
                "ordinal": 1,
                "origin_attempt_number": 1,
                "origin_outbox_id": stored.outbox_id,
            },
        )
    )
    assert (
        await temp_storage.observe_radio_part(
            started, _WORKER, 1, handoff_id=handoff.encode()
        )
        is None
    )


async def test_malformed_accepted_reference_does_not_disappear_at_ceiling(temp_storage):
    """A corrupt handoff never becomes message-only proof after erasure."""
    import json

    stored, provenance = await _seed_reserved_claim(temp_storage)
    await _bind_and_progress(temp_storage, provenance, ["accepted"] * 3)
    raw = await temp_storage._read_one(
        "SELECT parts_json FROM radio_attempt_progress WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert raw is not None
    parts = json.loads(raw["parts_json"])
    parts[0]["handoff_id"] = "{not-a-valid-reference}"
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=?",
        (json.dumps(parts), stored.outbox_id),
    )
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.append_delivery_observation(
        _observation("whole-message-after-invalid-part", provenance)
    )
    verdict = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert verdict.verdict == "unresolved"
    assert verdict.reasons == ("unreadable_handoff_reference",)


async def test_explicit_new_retry_failure_supersedes_stale_uncertainty_fact(
    temp_storage,
):
    """Supersession must be durable, not only a newer diagnostic row."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await temp_storage._write(
        "INSERT INTO delivery_handoff_uncertainty "
        "(outbox_id, attempt_number, state) "
        "VALUES (?, 1, 'uncertain_native_handoff')",
        (stored.outbox_id,),
    )
    assert await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=(
            datetime.now(timezone.utc) + timedelta(seconds=60)
        ).isoformat(),
        attempt_number=1,
        expected_worker_id=_WORKER,
        failure_kind_detail="adapter_transient",
    )
    historical = await temp_storage._read_one(
        "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
        "WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert historical == {"attempt_number": 1, "state": "superseded_native_handoff"}
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.failure_kind_detail == "adapter_transient"
    assert not await temp_storage.append_delivery_observation(
        _observation("late-not-uncertain", provenance)
    )

    # The ceiling deletes the free-form override. The supersession must
    # remain the only surviving fact and must not revive uncertainty.
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    surviving = await temp_storage._read_one(
        "SELECT attempt_number, state FROM delivery_handoff_uncertainty "
        "WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert surviving == {"attempt_number": 1, "state": "superseded_native_handoff"}
    assert (
        await temp_storage._read_one(
            "SELECT 1 FROM delivery_evidence_diagnostics "
            "WHERE family='outbox' AND row_id=?",
            (stored.outbox_id,),
        )
        is None
    )
    purged = await temp_storage.get_outbox_item(stored.outbox_id)
    assert purged is not None and purged.failure_kind_detail is None
    assert not await temp_storage.append_delivery_observation(
        _observation("late-after-purge", provenance)
    )


@pytest.mark.parametrize("completion", ["unknown", "accepted"])
async def test_in_flight_completion_with_prior_unreadable_handoff_stays_recordable(
    temp_storage, completion
):
    """Unreadable earlier evidence cannot erase an independently granted completion."""
    import json

    from medre.core.storage.radio import StoredRadioAttempt
    from medre.core.storage.radio_handoff import RadioLocalHandoff

    stored, provenance = await _seed_reserved_claim(temp_storage)
    bound = await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    assert bound is not None
    first = await temp_storage.begin_radio_part(bound, _WORKER, 1)
    assert first is not None
    accepted = await temp_storage.observe_radio_part(
        first,
        _WORKER,
        1,
        handoff_id=RadioLocalHandoff(1, "native-1", "ch-0", "local_transport").encode(),
    )
    assert accepted is not None
    started = await temp_storage.begin_radio_part(accepted, _WORKER, 2)
    assert started is not None
    corrupted_parts = list(started.progress.parts)
    corrupted_parts[0] = dataclass_replace(
        corrupted_parts[0], handoff_id="{bad-handoff}"
    )
    corrupted_progress = dataclass_replace(
        started.progress, parts=tuple(corrupted_parts)
    )
    corrupted_started = StoredRadioAttempt(
        provenance, corrupted_progress, started.revision
    )
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=?",
        (
            json.dumps(
                [
                    {
                        "ordinal": part.ordinal,
                        "state": part.state,
                        "handoff_id": part.handoff_id,
                    }
                    for part in corrupted_parts
                ]
            ),
            stored.outbox_id,
        ),
    )
    now = await _install_elapsed_ceiling_for_handoff(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    handoff = (
        RadioLocalHandoff(2, "native-2", "ch-0", "local_transport").encode()
        if completion == "accepted"
        else None
    )
    finished = await temp_storage.observe_radio_part(
        corrupted_started, _WORKER, 2, handoff_id=handoff
    )
    assert finished is not None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.parts[0].handoff_id == "{}"
    assert summary.parts[1].state == completion
    verdict = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert verdict.verdict == "unresolved"
    assert verdict.reasons == ("unreadable_handoff_reference",)


class _FixedResolutionResolver:
    """Return one prepared resolution and record the requested attempt."""

    def __init__(self, resolution):
        self.resolution = resolution
        self.calls: list[int] = []

    async def resolve(self, item, attempt_number):
        self.calls.append(attempt_number)
        return self.resolution


def _denied_item(**overrides) -> DeliveryOutboxItem:
    fields = {
        "outbox_id": "obox-content-denied",
        "event_id": "evt-content-denied",
        "route_id": "route-content-denied",
        "delivery_plan_id": "plan-content-denied",
        "target_adapter": "radio",
        "target_channel": "ch-0",
        "attempt_number": 1,
        "status": "retry_wait",
    }
    fields.update(overrides)
    return DeliveryOutboxItem(**fields)


async def test_content_denied_handoff_skips_an_unstarted_generation() -> None:
    lifecycle = DeliveryLifecycleService()
    resolver = _FixedResolutionResolver(None)
    item = _denied_item(attempt_number=0)
    assert (
        await lifecycle.reconcile_content_denied_handoff(
            MagicMock(), item, resolver, existing_receipt_count=0
        )
        is None
    )
    assert resolver.calls == []


async def test_content_denied_handoff_returns_unreadable_evidence() -> None:
    lifecycle = DeliveryLifecycleService()
    resolution = UncertainHandoffResolution(
        verdict="unresolved",
        attempt_number=1,
        uncertain=True,
        reasons=("resolution_evidence_unavailable",),
        has_checkpoint=False,
    )
    resolver = _FixedResolutionResolver(resolution)
    result = await lifecycle.reconcile_content_denied_handoff(
        MagicMock(), _denied_item(), resolver, existing_receipt_count=0
    )
    assert result is resolution
    assert resolver.calls == [1]


async def test_content_denied_handoff_settles_positive_evidence_with_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = DeliveryLifecycleService()
    resolution = UncertainHandoffResolution(
        verdict="delivered_with_evidence",
        attempt_number=1,
        uncertain=False,
        reasons=("radio_handoff_complete",),
        has_checkpoint=True,
        accepted_parts=1,
        uncertain_parts=0,
        unsent_parts=0,
        observation_count=0,
        checkpoint_revision=2,
    )
    finalization = RetryAttemptFinalization(
        outcome="accepted",
        receipt_id="rcpt-content-denied",
        failure_kind=None,
        attempt_number=1,
    )
    settle = AsyncMock(return_value=finalization)
    monkeypatch.setattr(lifecycle, "_settle_resolved_handoff", settle)
    resolver = _FixedResolutionResolver(resolution)
    item = _denied_item()

    assert (
        await lifecycle.reconcile_content_denied_handoff(
            MagicMock(), item, resolver, existing_receipt_count=0
        )
        is finalization
    )
    settle.assert_awaited_once()
    assert settle.await_args.args[1:] == (item, 1, resolution)
    assert settle.await_args.kwargs == {"expected_receipt_count": 0}

    settle.reset_mock()
    assert (
        await lifecycle.reconcile_content_denied_handoff(
            MagicMock(), item, resolver, existing_receipt_count=1
        )
        is finalization
    )
    settle.assert_awaited_once()
    assert settle.await_args.kwargs == {"expected_receipt_count": 1}


async def test_attempt_receipt_list_is_scoped_to_one_generation() -> None:
    receipts = [
        DeliveryReceipt(
            receipt_id="rcpt-attempt-1",
            event_id="evt-content-denied",
            delivery_plan_id="plan-content-denied",
            target_adapter="radio",
            outbox_id="obox-content-denied",
            attempt_number=1,
            status="failed",
        ),
        DeliveryReceipt(
            receipt_id="rcpt-attempt-2",
            event_id="evt-content-denied",
            delivery_plan_id="plan-content-denied",
            target_adapter="radio",
            outbox_id="obox-content-denied",
            attempt_number=2,
            status="failed",
        ),
    ]
    storage = MagicMock()
    storage.list_receipts_for_outbox = AsyncMock(return_value=receipts)
    listed = await DeliveryLifecycleService().list_attempt_receipts(
        storage, "obox-content-denied", 1
    )
    assert [receipt.receipt_id for receipt in listed] == ["rcpt-attempt-1"]
    storage.list_receipts_for_outbox.assert_awaited_once_with("obox-content-denied")
