"""Native receipt provenance and queue/completion separation after ceiling denial."""

from __future__ import annotations

import msgspec
import pytest

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events import DeliveryReceipt
from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.handoff_resolution import (
    _claimed_row,
    _past,
    _seed_reserved_claim,
)
from tests.helpers.storage_outbox import append_receipt_with_parent


def _receipt(
    provenance, status: str, *, receipt_id: str = "held-receipt"
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        source="retry",
        status=status,
    )


async def test_queued_receipt_cannot_be_promoted_to_sent(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    receipt = _receipt(provenance, "queued")
    await append_receipt_with_parent(temp_storage, receipt)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    result = await DeliveryLifecycleService().settle_committed_acceptance(
        temp_storage, item, attempt_number=1, receipts=[receipt]
    )
    assert result.outcome == "queued"
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None
    assert after.status == "queued"
    assert after.receipt_id == receipt.receipt_id
    assert after.active_attempt is None


async def test_receipt_provenance_must_match_before_queued_or_sent_settlement(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    for status in ("queued", "sent"):
        for fields in (
            {"target_channel": "foreign-room"},
            {"event_id": "foreign-event"},
        ):
            receipt = _receipt(provenance, status, receipt_id=f"foreign-{status}")
            receipt = msgspec.structs.replace(receipt, **fields)
            with pytest.raises(StorageError, match="provenance contradicts"):
                await DeliveryLifecycleService().settle_committed_acceptance(
                    temp_storage, item, attempt_number=1, receipts=[receipt]
                )
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "in_progress"


async def test_queued_receipt_settlement_fences_a_late_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    stored, provenance = await _seed_reserved_claim(temp_storage)
    receipt = _receipt(provenance, "queued")
    await append_receipt_with_parent(temp_storage, receipt)
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    extra = _receipt(provenance, "failed", receipt_id="newer-failure")
    await append_receipt_with_parent(temp_storage, extra)
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptCommitRejected

    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().settle_committed_acceptance(
            temp_storage, item, attempt_number=1, receipts=[receipt]
        )
    after = await temp_storage.get_outbox_item(stored.outbox_id)
    assert after is not None and after.status == "in_progress"


async def test_in_place_scope_rewrite_rejects_stale_handoff_settlement(
    temp_storage: SQLiteStorage,
) -> None:
    """A scope rewrite with the same row count invalidates a resolver verdict."""
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptCommitRejected
    from medre.core.evidence.handoff_resolution import UncertainHandoffResolution
    from tests.helpers.handoff_resolution import _observation

    stored, provenance = await _seed_reserved_claim(temp_storage)
    observation = _observation(
        "scope-rewrite",
        provenance,
        radio_packet={
            "ordinal": 1,
            "origin_outbox_id": provenance.outbox_id,
            "origin_attempt_number": provenance.attempt_number,
        },
    )
    assert await temp_storage.append_delivery_observation(observation)
    evidence, version_before = await temp_storage.read_handoff_observation_snapshot(
        provenance.outbox_id, provenance.attempt_number
    )
    assert len(evidence) == 1 and version_before == 1
    item = await temp_storage.get_outbox_item(stored.outbox_id)
    assert item is not None
    decision = UncertainHandoffResolution(
        verdict="delivered_with_evidence",
        attempt_number=1,
        uncertain=False,
        reasons=("provider_acceptance",),
        has_checkpoint=False,
        observation_count=1,
        observation_scope_revision=version_before,
    )
    await temp_storage._write(
        "UPDATE delivery_observation_scopes SET scope_json=? WHERE observation_id=?",
        ('{"radio_packet":null}', observation.observation_id),
    )
    _, version_after = await temp_storage.read_handoff_observation_snapshot(
        provenance.outbox_id, provenance.attempt_number
    )
    assert version_after == version_before + 1
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService()._settle_resolved_handoff(
            temp_storage, item, 1, decision
        )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"


async def test_scope_revision_applies_to_reservation_and_abandonment(
    temp_storage: SQLiteStorage,
) -> None:
    from medre.core.engine.pipeline.delivery_lifecycle import RetryAttemptCommitRejected
    from tests.helpers.handoff_resolution import _observation

    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.append_delivery_observation(
        _observation(
            "scope-dual-guard",
            provenance,
            radio_packet={
                "ordinal": 1,
                "origin_outbox_id": provenance.outbox_id,
                "origin_attempt_number": provenance.attempt_number,
            },
        )
    )
    _, initial = await temp_storage.read_handoff_observation_snapshot(
        provenance.outbox_id, 1
    )
    assert initial == 1
    await temp_storage._write(
        "UPDATE delivery_observation_scopes SET scope_json=? WHERE observation_id=?",
        ('{"radio_packet":null}', "scope-dual-guard"),
    )
    _, updated = await temp_storage.read_handoff_observation_snapshot(
        provenance.outbox_id, 1
    )
    assert updated == initial + 1
    # Finalize attempt 1 so no reservation owns the row, then re-claim it:
    # the guards below must decide on the scope revision, not on an
    # already-active reservation.
    await temp_storage.mark_outbox_retry_wait(
        stored.outbox_id,
        next_attempt_at=_past().isoformat(),
        failure_kind="ambiguous_dispatch",
        attempt_number=1,
        expected_worker_id=stored.worker_id,
    )
    item = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    assert item.active_attempt is None and item.attempt_number == 1
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().abandon_retry_outbox(
            temp_storage,
            item,
            error_summary="content_authority_denied",
            expected_observation_scope_revision=initial,
        )
    # A stale scope revision cannot reserve attempt 2 under the same write.
    assert (
        await temp_storage.reserve_outbox_attempt(
            stored.outbox_id,
            item.worker_id,
            1,
            expected_observation_scope_revision=initial,
        )
        is None
    )
    # The updated revision reserves the next attempt in the same guard.
    assert (
        await temp_storage.reserve_outbox_attempt(
            stored.outbox_id,
            item.worker_id,
            1,
            expected_observation_scope_revision=updated,
        )
        == 2
    )
