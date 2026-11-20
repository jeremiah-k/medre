"""Exact-attempt evidence, reservation, and historical receipt ordering fences."""

from __future__ import annotations

import pytest
from msgspec.structs import force_setattr, replace

from medre.core.delivery_authority import dispatch_source_matches_attempt
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events import DeliveryReceipt
from medre.core.evidence.handoff_resolution import (
    UNCERTAIN_NATIVE_HANDOFF_DETAIL,
    EvidenceBackedHandoffResolver,
    UncertainHandoffResolution,
    resolve_uncertain_handoff,
)
from medre.core.planning.delivery_plan import RetryPolicy
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
    for _ in range(2):
        upgraded = SQLiteStorage(path)
        await upgraded.initialize()
        try:
            assert await upgraded.get_outbox_item(stored.outbox_id) is not None
            assert (
                await upgraded._read_all("SELECT * FROM delivery_handoff_settlements")
                == []
            )
        finally:
            await upgraded.close()
