"""Execution-time privacy, atomic terminal-fence and maintenance cadence controls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.config.model import CeilingSweepConfig, RetryConfig
from medre.core.engine.pipeline.delivery_lifecycle import (
    DeliveryLifecycleService,
    RetryAttemptCommitRejected,
)
from medre.core.events import DeliveryReceipt, NativeMessageRef
from medre.core.evidence.handoff_resolution import EvidenceBackedHandoffResolver
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.retry import RetryWorker
from tests.helpers.ceiling_audit import _admit_with_ceiling, _install_elapsed_policy
from tests.helpers.handoff_resolution import (
    _WORKER,
    _claimed_row,
    _observation,
    _plan,
    _seed_reserved_claim,
)
from tests.helpers.initial_outbox import _pipeline
from tests.helpers.storage_outbox import append_receipt_with_parent


async def test_native_reference_write_scrubs_elapsed_ceiling_before_first_sweep(
    temp_storage: SQLiteStorage,
) -> None:
    """An unpurged elapsed policy must deny new provider metadata at INSERT."""
    expired, current = "native-write-expired", "native-write-current"
    await _admit_with_ceiling(temp_storage, expired)
    await _admit_with_ceiling(temp_storage, current)
    await temp_storage._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        ((datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(), expired),
    )
    for event_id, expected in ((expired, {}), (current, {"private": "marker"})):
        ref = NativeMessageRef(
            id=f"ref-{event_id}",
            event_id=event_id,
            adapter="matrix",
            native_channel_id="room",
            native_message_id=f"message-{event_id}",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            metadata={"private": "marker"},
        )
        await temp_storage.store_native_ref(ref)
        row = await temp_storage._read_one(
            "SELECT metadata FROM native_message_refs WHERE id=?", (ref.id,)
        )
        assert row is not None
        assert (row["metadata"] == "{}") is (not bool(expected))
        resolved = await temp_storage.get_native_ref(
            "matrix", "room", f"message-{event_id}"
        )
        assert resolved is not None and resolved.event_id == event_id
        assert resolved.metadata == expected
    receipt = await temp_storage._read_one(
        "SELECT 1 FROM event_content_ceiling_log WHERE event_id=?", (expired,)
    )
    assert receipt is None


async def test_matching_count_cannot_abandon_an_accepted_receipt(
    temp_storage: SQLiteStorage,
) -> None:
    """The terminal CAS must reject acceptance even if the caller saw its count."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    item = await _claimed_row(temp_storage, stored.outbox_id, "reclaimer")
    await append_receipt_with_parent(
        temp_storage,
        DeliveryReceipt(
            receipt_id="accepted-between-reads",
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            status="sent",
            outbox_id=provenance.outbox_id,
            attempt_number=provenance.attempt_number,
            source="retry",
        ),
    )
    with pytest.raises(RetryAttemptCommitRejected):
        await DeliveryLifecycleService().abandon_retry_outbox(
            temp_storage,
            item,
            error_summary="content_authority_denied",
            expected_observation_count=0,
            expected_receipt_count=1,
        )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None and row.status == "in_progress"


def test_ceiling_maintenance_wakes_before_long_retry_poll(temp_storage) -> None:
    """A configured 60-second purge must not sleep behind a one-hour retry scan."""
    runner, _, _ = _pipeline(temp_storage)
    worker = RetryWorker(
        temp_storage,
        runner,
        None,
        retry_config=RetryConfig(
            enabled=False,
            interval_seconds=3600,
            ceiling_sweep=CeilingSweepConfig(enabled=True, interval_seconds=60),
        ),
        initial_dispatch=False,
    )
    worker._next_ceiling_sweep_at = None
    assert worker._next_poll_timeout() == 0
    worker._next_ceiling_sweep_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert 0 < worker._next_poll_timeout() <= 60
    worker._next_ceiling_sweep_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert worker._next_poll_timeout() == 0


async def test_nullable_native_identity_is_atomic_even_without_preflight_lookup(
    temp_storage: SQLiteStorage,
) -> None:
    """The INSERT itself prevents a second owner for a NULL-channel native ID."""
    from medre.core.storage.sqlite._native_ref import _native_ref_insert_params
    from medre.core.storage.sqlite.statements import _INSERT_NATIVE_REF

    for event_id in ("null-native-a", "null-native-b"):
        await _admit_with_ceiling(temp_storage, event_id)
    first = NativeMessageRef(
        id="native-null-first",
        event_id="null-native-a",
        adapter="radio",
        native_channel_id=None,
        native_message_id="shared-native-id",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={},
    )
    second = NativeMessageRef(
        id="native-null-second",
        event_id="null-native-b",
        adapter="radio",
        native_channel_id=None,
        native_message_id="shared-native-id",
        native_thread_id=None,
        native_relation_id=None,
        direction="outbound",
        metadata={},
    )
    await temp_storage.store_native_ref(first)
    await temp_storage._write(_INSERT_NATIVE_REF, _native_ref_insert_params(second))
    refs = await temp_storage._read_all(
        "SELECT event_id FROM native_message_refs WHERE adapter=? "
        "AND native_channel_id IS NULL AND native_message_id=?",
        ("radio", "shared-native-id"),
    )
    assert [row["event_id"] for row in refs] == ["null-native-a"]


async def test_historical_queued_receipt_does_not_block_newer_attempt_abandonment(
    temp_storage: SQLiteStorage,
) -> None:
    """Acceptance is attempt-scoped: attempt 1 cannot freeze attempt 2 forever."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    await append_receipt_with_parent(
        temp_storage,
        DeliveryReceipt(
            receipt_id="historical-queued-attempt-1",
            event_id=provenance.event_id,
            delivery_plan_id=provenance.delivery_plan_id,
            target_adapter=provenance.target_adapter,
            target_channel=provenance.target_channel,
            status="queued",
            outbox_id=provenance.outbox_id,
            attempt_number=1,
            source="retry",
        ),
    )
    # A stale-queue recovery reserved a later generation. Its outcome is
    # independent of the already-consumed attempt-1 acceptance.
    await temp_storage._write(
        "UPDATE delivery_outbox SET attempt_number=1, active_attempt=2, "
        "status='in_progress' WHERE outbox_id=?",
        (stored.outbox_id,),
    )
    assert await temp_storage.mark_outbox_abandoned(
        stored.outbox_id,
        error_summary="suppressed",
        attempt_number=2,
        expected_receipt_count=0,
    )
    row = await temp_storage.get_outbox_item(stored.outbox_id)
    assert row is not None
    assert row.status == "abandoned"
    assert row.attempt_number == 2


async def test_unreadable_checkpoint_blocks_message_level_settlement_after_purge(
    temp_storage: SQLiteStorage,
) -> None:
    """An undecodable grant must not become 'no checkpoint' after erasure."""
    stored, provenance = await _seed_reserved_claim(temp_storage)
    assert await temp_storage.bind_radio_attempt(provenance, _WORKER, _plan())
    await temp_storage._write(
        "UPDATE radio_attempt_progress SET plan_json=? WHERE outbox_id=?",
        ('{"unreadable":true}', stored.outbox_id),
    )
    now = await _install_elapsed_policy(temp_storage, provenance.event_id)
    await temp_storage.enforce_content_ceilings(now=now)
    assert await temp_storage.get_radio_attempt(stored.outbox_id, 1) is None
    summary = await temp_storage.get_radio_attempt_evidence(stored.outbox_id, 1)
    assert summary is not None
    assert summary.parts[0].state == "accepted"
    assert summary.parts[0].handoff_id == "{}"
    assert await temp_storage.append_delivery_observation(
        _observation("whole-message-after-unreadable-checkpoint", provenance)
    )
    verdict = await EvidenceBackedHandoffResolver(temp_storage).resolve(stored, 1)
    assert verdict.verdict == "unresolved"
    assert verdict.reasons == ("unreadable_handoff_reference",)
