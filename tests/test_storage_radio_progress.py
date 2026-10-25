"""Durable radio checkpoints fence native calls without acquiring lifecycle authority."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace

import pytest

from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.backend import StorageError
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


@pytest.fixture
async def storage(tmp_path):
    """An isolated file-backed store exercises the real connection owner."""
    store = SQLiteStorage(db_path=str(tmp_path / "radio.db"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def claim(storage, *, generation=1, replay_run_id=None):
    """Admit an owned outbox row through production validation."""
    item = replace(
        make_outbox_item(status="in_progress", attempt_number=generation),
        worker_id="sender-1",
        dispatch_source="replay" if replay_run_id else "live",
        replay_run_id=replay_run_id,
    )
    item = await create_outbox_item_with_parent(
        storage, item, allocate_new_generation=replay_run_id is not None
    )
    return item, DeliveryAttemptProvenance(
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        source=item.dispatch_source,
        replay_run_id=replay_run_id,
    )


def plan():
    """Use a real multibyte, prefixed plan with several ordered native packets."""
    return plan_radio_text("café α " * 16, max_text_bytes=40, prefix="[North] ")


async def test_plan_and_each_transition_are_durable_without_parent_completion(storage):
    """Local packet acceptance preserves parent status and creates no receipts."""
    item, provenance = await claim(storage)
    wire = plan()
    initial = await storage.bind_radio_attempt(provenance, "sender-1", wire)
    assert initial is not None and initial.revision == 0
    assert await storage.bind_radio_attempt(provenance, "sender-1", wire) is None
    checkpoint = initial
    for ordinal in range(1, len(wire.parts) + 1):
        submitting = await storage.begin_radio_part(checkpoint, "sender-1", ordinal)
        assert submitting is not None
        observed = await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
        assert observed["parts"][ordinal - 1]["state"] == "submitting"
        checkpoint = await storage.observe_radio_part(
            submitting, "sender-1", ordinal, handoff_id=f"radio:north:{ordinal}"
        )
        assert checkpoint is not None
    assert checkpoint.progress.handoff_complete
    assert checkpoint.revision == 2 * len(wire.parts)
    assert checkpoint.progress.plan == wire
    assert await storage.get_outbox_item(item.outbox_id) == item
    assert await storage.list_receipts_for_event(item.event_id) == []


async def test_only_one_concurrent_begin_grants_a_native_call(storage):
    """Two send owners cannot acquire the same packet from one retained revision."""
    _, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    results = await asyncio.gather(
        storage.begin_radio_part(initial, "sender-1", 1),
        storage.begin_radio_part(initial, "sender-1", 1),
    )
    assert sum(result is not None for result in results) == 1
    submitting = next(result for result in results if result is not None)
    accepted = await storage.observe_radio_part(
        submitting, "sender-1", 1, handoff_id="native:1"
    )
    assert accepted is not None
    assert (
        await storage.observe_radio_part(
            submitting, "sender-1", 1, handoff_id="conflicting:2"
        )
        is None
    )


async def test_restart_preserves_prefix_and_commits_unknown_before_returning(storage):
    """A persisted in-flight call blocks replay and rejects a late old observation."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    submitting = await storage.begin_radio_part(initial, "sender-1", 1)
    accepted = await storage.observe_radio_part(
        submitting, "sender-1", 1, handoff_id="native:1"
    )
    interrupted = await storage.begin_radio_part(accepted, "sender-1", 2)
    await storage.close()
    await storage.initialize()
    before = await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
    assert before["parts"][1]["state"] == "submitting"  # inspection is read-only
    recovered = await storage.recover_radio_attempt(provenance, "sender-1")
    assert recovered.progress.parts[0].handoff_id == "native:1"
    assert recovered.progress.parts[1].state == "unknown"
    assert recovered.progress.next_unsent is None
    assert recovered.revision == interrupted.revision + 1
    assert await storage.recover_radio_attempt(provenance, "sender-1") == recovered
    assert (
        await storage.observe_radio_part(
            interrupted, "sender-1", 2, handoff_id="late:2"
        )
        is None
    )
    after = await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
    assert after == recovered.progress.to_data()
    with pytest.raises(ValueError, match="not eligible"):
        await storage.begin_radio_part(recovered, "sender-1", 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "other"),
        ("delivery_plan_id", "other"),
        ("target_adapter", "other"),
        ("target_channel", "other"),
        ("outbox_id", "missing"),
        ("attempt_number", 2),
        ("source", "retry"),
        ("replay_run_id", "other-run"),
    ],
)
async def test_full_claim_fences_binding_and_recovery(storage, field, value):
    """A matching outbox ID alone is insufficient for a replay/source claim."""
    _, provenance = await claim(storage, replay_run_id="run-1")
    # A retry can retain replay lineage; an arbitrary source cannot borrow it.
    changed = replace(provenance, **{field: value})
    assert await storage.bind_radio_attempt(changed, "sender-1", plan()) is None
    assert await storage.recover_radio_attempt(changed, "sender-1") is None
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    assert initial is not None


async def test_wrong_owner_and_newer_sibling_fence_every_writer(storage):
    """Ownership and generation are checked in the checkpoint transaction."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    assert await storage.begin_radio_part(initial, "sender-2", 1) is None
    assert await storage.recover_radio_attempt(provenance, "sender-2") is None
    sibling = replace(item, outbox_id="new-outbox", attempt_number=2)
    await storage.create_outbox_item(sibling)
    assert await storage.begin_radio_part(initial, "sender-1", 1) is None
    assert await storage.recover_radio_attempt(provenance, "sender-1") is None


@pytest.mark.parametrize("native_state", ["submitting", "accepted", "unknown"])
async def test_new_outbox_generation_cannot_erase_prior_native_work(
    storage, native_state
):
    """Reallocating an outbox ID cannot bypass uncertainty or duplicate acceptance."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    submitted = await storage.begin_radio_part(initial, "sender-1", 1)
    if native_state != "submitting":
        await storage.observe_radio_part(
            submitted,
            "sender-1",
            1,
            handoff_id="native:1" if native_state == "accepted" else None,
        )
    sibling = replace(item, outbox_id="new-outbox", attempt_number=2)
    await storage.create_outbox_item(sibling)
    replacement = replace(provenance, outbox_id=sibling.outbox_id, attempt_number=2)
    assert await storage.bind_radio_attempt(replacement, "sender-1", plan()) is None


async def test_wholly_unsent_generation_can_be_rebound_only_to_exact_wire_plan(storage):
    """Claim replacement before native work keeps exact persisted bytes."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    sibling = replace(item, outbox_id="new-outbox", attempt_number=2)
    await storage.create_outbox_item(sibling)
    replacement = replace(provenance, outbox_id=sibling.outbox_id, attempt_number=2)
    changed = plan_radio_text("different", max_text_bytes=40)
    with pytest.raises(StorageError, match="wire plan"):
        await storage.bind_radio_attempt(replacement, "sender-1", changed)
    rebound = await storage.bind_radio_attempt(replacement, "sender-1", plan())
    assert rebound.progress.plan == initial.progress.plan
    assert rebound.progress.next_unsent == 1


@pytest.mark.parametrize(
    "column,value",
    [
        ("parts_json", "[]"),
        ("parts_json", '[{"ordinal":1,"state":"accepted"}]'),
        ("parts_json", "[1]"),
        ("provenance_json", "{}"),
        ("revision", 7),
        ("plan_fingerprint", "0" * 64),
    ],
)
async def test_corrupted_evidence_fails_closed(storage, column, value):
    """Malformed retained state cannot become a new send grant or valid report."""
    item, provenance = await claim(storage)
    await storage.bind_radio_attempt(provenance, "sender-1", plan())
    # Deliberate fault injection; column identifiers come only from this test table.
    storage._db.execute(
        f"UPDATE radio_attempt_progress SET {column}=? WHERE outbox_id=?",
        (value, item.outbox_id),
    )
    storage._db.commit()
    with pytest.raises(StorageError, match="evidence is invalid"):
        await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
    with pytest.raises(StorageError, match="evidence is invalid"):
        await storage.recover_radio_attempt(provenance, "sender-1")


async def test_failed_checkpoint_rolls_back_and_does_not_grant_send(storage):
    """A SQLite write failure leaves the prior checkpoint intact and reusable."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    storage._db.executescript("""
        CREATE TRIGGER reject_radio_update BEFORE UPDATE ON radio_attempt_progress
        BEGIN SELECT RAISE(ABORT, 'injected write failure'); END;
    """)
    with pytest.raises(StorageError, match="transaction failed"):
        await storage.begin_radio_part(initial, "sender-1", 1)
    assert (
        await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
        == initial.progress.to_data()
    )
    storage._db.execute("DROP TRIGGER reject_radio_update")
    assert await storage.begin_radio_part(initial, "sender-1", 1) is not None


async def test_readonly_inspection_never_grants_a_send(storage):
    """Inspection preserves submitting; an attempted write through mode=ro fails."""
    item, provenance = await claim(storage)
    initial = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    await storage.begin_radio_part(initial, "sender-1", 1)
    readonly = await SQLiteStorage.open_readonly(storage._db_path)
    try:
        snapshot = await readonly.get_radio_attempt_snapshot(item.outbox_id, 1)
        assert snapshot["parts"][0]["state"] == "submitting"
        with pytest.raises(StorageError):
            await readonly.recover_radio_attempt(provenance, "sender-1")
        assert await storage.get_radio_attempt_snapshot(item.outbox_id, 1) == snapshot
    finally:
        await readonly.close()


@pytest.mark.parametrize("provenance,progress,revision", [(None, None, 0), (1, 2, 0)])
def test_checkpoint_rejects_untyped_values(provenance, progress, revision):
    """Malformed API input has a domain validation error before any field access."""
    with pytest.raises(ValueError, match="typed"):
        StoredRadioAttempt(provenance, progress, revision)


async def test_reserved_retry_generation_is_the_only_eligible_claim(storage):
    """An active retry reservation fences the earlier finalized generation."""
    _, provenance = await claim(storage)
    assert (
        await storage.reserve_outbox_attempt(provenance.outbox_id, "sender-1", 1) == 2
    )
    assert await storage.bind_radio_attempt(provenance, "sender-1", plan()) is None
    retry = replace(provenance, attempt_number=2, source="retry")
    checkpoint = await storage.bind_radio_attempt(retry, "sender-1", plan())
    assert checkpoint is not None
    assert checkpoint.progress.attempt_number == 2


async def test_missing_required_radio_table_is_rejected_before_ddl(tmp_path):
    """A stale prerelease database is preserved byte-for-byte for explicit reset."""
    import hashlib
    import sqlite3

    from medre.core.storage.backend import PreReleaseSchemaMismatchError

    path = tmp_path / "older.db"
    initial = SQLiteStorage(db_path=str(path))
    await initial.initialize()
    await initial.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE radio_attempt_progress")
        db.commit()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    newer = SQLiteStorage(db_path=str(path))
    try:
        with pytest.raises(
            PreReleaseSchemaMismatchError, match="radio_attempt_progress"
        ):
            await newer.initialize()
    finally:
        await newer.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(path)) as db:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='radio_attempt_progress'"
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("field", ["ordinal", "state", "handoff_id"])
async def test_missing_part_fields_are_not_reconstructed_from_defaults(storage, field):
    """An incomplete stored part cannot masquerade as a fresh unsent packet."""
    item, provenance = await claim(storage)
    checkpoint = await storage.bind_radio_attempt(provenance, "sender-1", plan())
    data = checkpoint.progress.to_data()
    del data["parts"][0][field]
    storage._db.execute(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=?",
        (json.dumps(data["parts"]), item.outbox_id),
    )
    storage._db.commit()
    with pytest.raises(StorageError, match="evidence is invalid"):
        await storage.recover_radio_attempt(provenance, "sender-1")
