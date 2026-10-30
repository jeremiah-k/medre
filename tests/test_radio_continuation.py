"""Explicit tail continuation retains original evidence without automatic replay."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.contracts.delivery import AdapterHandoffResult
from medre.core.engine.pipeline.radio_delivery import deliver_radio_plan
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_progress import RadioAttemptProgress
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.rendering.renderer import RenderingResult
from medre.core.storage.backend import StorageError
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


@pytest.fixture
async def owned(tmp_path):
    """Use real file-backed storage and an independently admitted in-progress claim."""
    storage = SQLiteStorage(db_path=str(tmp_path / "continuation.db"))
    await storage.initialize()
    item = await create_outbox_item_with_parent(
        storage,
        make_outbox_item(status="pending", attempt_number=0, target_channel="0"),
    )
    rows = await storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "first"
    )
    assert any(row.outbox_id == item.outbox_id for row in rows)
    assert await storage.reserve_outbox_attempt(item.outbox_id, "first", 0) == 1
    claim = DeliveryAttemptProvenance(
        item.event_id,
        item.delivery_plan_id,
        item.target_adapter,
        item.target_channel,
        item.outbox_id,
        1,
        "live",
    )
    wire = plan_radio_text("café α " * 12, prefix="[North] ", max_text_bytes=40)
    result = RenderingResult(
        event_id=item.event_id,
        target_adapter=item.target_adapter,
        target_channel="0",
        payload={"text": wire.parts[0].wire_text, "channel_index": 0},
        radio_plan=wire,
        attempt_provenance=claim,
    )
    try:
        yield storage, result
    finally:
        await storage.close()


async def stop_between_parts(storage, result, worker="first", *, resume_from=None):
    """Accept one local packet then fail before granting the next native call."""
    calls = []

    async def deliver(packet):
        if calls:
            raise ConnectionError("disconnected before next grant")
        await packet.before_native_send()
        calls.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_message_id="0",
            native_channel_id="0",
            confirmation_level="local_queue",
        )

    with pytest.raises(AdapterPermanentError, match="requires inspection"):
        await deliver_radio_plan(
            storage, result, worker, deliver, resume_from=resume_from
        )
    predecessor = await storage.get_radio_attempt(
        result.outbox_id, result.attempt_number
    )
    assert predecessor is not None
    return predecessor


async def reserve(storage, result, worker="next"):
    """Transfer through production lifecycle claim/reservation, never a row rewrite."""
    claim = result.attempt_provenance
    now = datetime.now(timezone.utc).isoformat()
    assert await storage.mark_outbox_retry_wait(
        claim.outbox_id, now, attempt_number=claim.attempt_number
    )
    rows = await storage.claim_due_outbox_items(now, worker)
    assert len(rows) == 1
    number = await storage.reserve_outbox_attempt(
        claim.outbox_id, worker, claim.attempt_number
    )
    assert number == claim.attempt_number + 1
    return replace(
        result,
        attempt_number=number,
        attempt_provenance=replace(claim, attempt_number=number, source="retry"),
    )


async def test_restart_continuation_sends_only_unsent_tail_and_keeps_original_origin(
    owned,
):
    """A new process inherits exact facts and aggregates their weakest local strength."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    path = storage._db_path
    await storage.close()
    await storage.initialize()
    assert storage._db_path == path
    current = await reserve(storage, first)
    sent = []

    async def deliver(packet):
        await packet.before_native_send()
        sent.append(packet.payload["text"])
        return AdapterHandoffResult(
            native_message_id=str(len(sent)),
            native_channel_id="0",
            confirmation_level="local_transport",
        )

    handoff = await deliver_radio_plan(
        storage, current, "next", deliver, resume_from=predecessor
    )
    assert sent == [part.wire_text for part in first.radio_plan.parts[1:]]
    assert handoff.confirmation_level == "local_queue"
    assert handoff.native_message_id is None and handoff.native_channel_id is None
    assert await storage.get_radio_attempt(first.outbox_id, 1) == predecessor
    saved = await storage.get_radio_attempt(first.outbox_id, 2)
    assert saved.progress.handoff_complete
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/radio-local-handoff.schema.json"
        ).read_text()
    )
    for part in saved.progress.parts:
        Draft202012Validator(schema).validate(json.loads(part.handoff_id))
    original = json.loads(saved.progress.parts[0].handoff_id)
    assert original["native_message_id"] == "0"
    assert original["origin"] == {"outbox_id": first.outbox_id, "attempt_number": 1}
    assert (await storage.get_outbox_item(first.outbox_id)).status == "in_progress"
    assert await storage.list_receipts_for_event(first.event_id) == []


async def test_normal_binding_and_same_generation_cannot_resume_partial_work(owned):
    """An explicit newer claim is mandatory; ordinary retry remains fail closed."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    assert (
        await storage.resume_radio_attempt(
            first.attempt_provenance, "first", first.radio_plan, predecessor
        )
        is None
    )
    current = await reserve(storage, first)
    assert (
        await storage.bind_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan
        )
        is None
    )
    assert (
        await storage.resume_radio_attempt(
            current.attempt_provenance, "stale", first.radio_plan, predecessor
        )
        is None
    )


async def test_concurrent_continuations_bind_once_and_fence_old_native_owner(owned):
    """Only one new checkpoint is granted; the old process cannot extend its prefix."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)
    outcomes = await asyncio.gather(
        *[
            storage.resume_radio_attempt(
                current.attempt_provenance, "next", first.radio_plan, predecessor
            )
            for _ in range(2)
        ]
    )
    assert sum(value is not None for value in outcomes) == 1
    assert await storage.begin_radio_part(predecessor, "first", 2) is None


@pytest.mark.parametrize("state", ["submitting", "unknown"])
async def test_uncertain_predecessor_is_never_resumed(owned, state):
    """Neither interrupted submission nor explicit uncertainty proves an unsent tail."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    predecessor = await storage.begin_radio_part(predecessor, "first", 2)
    if state == "unknown":
        predecessor = await storage.observe_radio_part(predecessor, "first", 2)
    current = await reserve(storage, first)
    with pytest.raises(StorageError, match="continuation evidence"):
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, predecessor
        )
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None


async def test_changed_wire_plan_or_forged_checkpoint_cannot_transfer(owned):
    """Storage independently verifies the predecessor and exact immutable wire plan."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)
    changed = plan_radio_text("changed text", max_text_bytes=40)
    with pytest.raises(StorageError, match="wire plan"):
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", changed, predecessor
        )
    fake = StoredRadioAttempt(
        predecessor.provenance,
        RadioAttemptProgress.bind(
            outbox_id=first.outbox_id, attempt_number=1, plan=first.radio_plan
        ),
        0,
    )
    assert (
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, fake
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "reply", "channel_index": 0, "reply_id": 123},
        {"text": "changed scope", "channel_index": 1},
        {"text": "coerced scope", "channel_index": False},
    ],
)
async def test_unsupported_or_changed_payload_never_reconstructs_native_extras(
    owned, payload
):
    """Text-only persistence cannot authorize a changed native command or channel."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)
    calls = []
    with pytest.raises(AdapterPermanentError, match="plain channel"):
        await deliver_radio_plan(
            storage,
            replace(current, payload=payload),
            "next",
            calls.append,
            resume_from=predecessor,
        )
    assert not calls
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None


async def test_multiple_transfers_keep_first_observing_attempt_for_each_part(owned):
    """Inherited facts are flattened to original acceptance, never relabeled on transfer."""
    storage, first = owned
    one = await stop_between_parts(storage, first)
    second = await reserve(storage, first)
    two = await stop_between_parts(storage, second, "next", resume_from=one)
    third = await reserve(storage, second, "third")
    saved = await storage.resume_radio_attempt(
        third.attempt_provenance, "third", first.radio_plan, two
    )
    assert [
        json.loads(part.handoff_id)["origin"]["attempt_number"]
        for part in saved.progress.parts[:2]
    ] == [1, 2]
    assert all(part.state == "unsent" for part in saved.progress.parts[2:])
    assert await storage.get_radio_attempt(first.outbox_id, 1) == one
    assert await storage.get_radio_attempt(first.outbox_id, 2) == two


async def test_insert_failure_rolls_back_transfer_without_mutating_original(owned):
    """A failed transaction grants no continuation and leaves immutable native facts intact."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)
    storage._db.executescript("""
        CREATE TRIGGER reject_continuation BEFORE INSERT ON radio_attempt_progress
        WHEN NEW.attempt_number > 1
        BEGIN SELECT RAISE(ABORT, 'injected continuation failure'); END;
    """)
    with pytest.raises(StorageError, match="transaction failed"):
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, predecessor
        )
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None
    assert await storage.get_radio_attempt(first.outbox_id, 1) == predecessor


@pytest.mark.parametrize(
    "damage", ["missing-origin", "changed-origin", "missing-fingerprint"]
)
async def test_corrupt_original_evidence_cannot_be_inherited(owned, damage):
    """A reference string must resolve to the same original accepted native fact."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    parts = predecessor.progress.to_data()["parts"]
    evidence = json.loads(parts[0]["handoff_id"])
    if damage == "missing-origin":
        evidence["origin"] = {"outbox_id": "absent", "attempt_number": 1}
    elif damage == "changed-origin":
        evidence["origin"] = {"outbox_id": first.outbox_id, "attempt_number": 2}
    else:
        evidence.pop("payload_fingerprint")
    parts[0]["handoff_id"] = json.dumps(evidence)
    storage._db.execute(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=? AND attempt_number=1",
        (json.dumps(parts), first.outbox_id),
    )
    storage._db.commit()
    predecessor = await storage.get_radio_attempt(first.outbox_id, 1)
    current = await reserve(storage, first)
    with pytest.raises(StorageError, match="continuation evidence"):
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, predecessor
        )
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None


async def test_fully_accepted_plan_is_never_resubmitted_by_continuation(owned):
    """A crash before parent completion cannot turn accepted work into an unsent tail."""
    storage, first = owned

    async def deliver(packet):
        await packet.before_native_send()
        return AdapterHandoffResult(
            native_channel_id="0", confirmation_level="local_transport"
        )

    await deliver_radio_plan(storage, first, "first", deliver)
    predecessor = await storage.get_radio_attempt(first.outbox_id, 1)
    current = await reserve(storage, first)
    assert (
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, predecessor
        )
        is None
    )


async def test_interruption_during_continued_native_call_holds_tail_again(owned):
    """An inherited prefix does not weaken uncertainty handling for a new native call."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)

    async def deliver(packet):
        await packet.before_native_send()
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await deliver_radio_plan(
            storage, current, "next", deliver, resume_from=predecessor
        )
    saved = await storage.get_radio_attempt(first.outbox_id, 2)
    assert [part.state for part in saved.progress.parts[:3]] == [
        "accepted",
        "unknown",
        "unsent",
    ]
    latest = await reserve(storage, current, "third")
    with pytest.raises(StorageError, match="continuation evidence"):
        await storage.resume_radio_attempt(
            latest.attempt_provenance, "third", first.radio_plan, saved
        )


async def test_readonly_checkpoint_selection_grants_no_continuation(owned):
    """Inspection can pin typed facts while leaving all submission authority with a writer."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    current = await reserve(storage, first)
    readonly = await SQLiteStorage.open_readonly(storage._db_path)
    try:
        assert await readonly.get_radio_attempt(first.outbox_id, 1) == predecessor
        with pytest.raises(StorageError, match="transaction failed"):
            await readonly.resume_radio_attempt(
                current.attempt_provenance, "next", first.radio_plan, predecessor
            )
    finally:
        await readonly.close()
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("ordinal", False),
        ("ordinal", 129),
        ("ordinal", 2),
        ("native_message_id", 0),
        ("native_channel_id", True),
        ("confirmation_level", "recipient_guaranteed"),
        ("origin", {"outbox_id": "original", "attempt_number": False}),
        ("origin", {"outbox_id": "", "attempt_number": 1}),
        ("origin", {"outbox_id": "original", "attempt_number": 1, "extra": True}),
        ("payload_fingerprint", "forged"),
        ("extra", "unrecognized"),
    ],
)
def test_local_handoff_reference_rejects_malformed_scope_and_authority(field, value):
    """Native aliases and malformed origin data cannot become inherited acceptance."""
    import msgspec

    from medre.core.rendering.radio_progress import RadioPartProgress
    from medre.core.storage.radio_handoff import RadioLocalHandoff

    claim = DeliveryAttemptProvenance(
        "event", "plan", "adapter", "0", "original", 1, "live"
    )
    data = {
        "ordinal": 1,
        "native_message_id": "0",
        "native_channel_id": "0",
        "confirmation_level": "local_transport",
    }
    data[field] = value
    with pytest.raises((ValueError, msgspec.DecodeError)):
        RadioLocalHandoff.from_part(
            RadioPartProgress(1, "accepted", json.dumps(data)), claim
        )


@pytest.mark.parametrize(
    "corrupt_handoff",
    [
        "not-json",
        "null",
        {
            "ordinal": 1,
            "native_message_id": 123,
            "native_channel_id": "0",
            "confirmation_level": "local_transport",
        },
    ],
)
async def test_invalid_retained_local_handoff_fails_permanently_before_binding(
    owned, corrupt_handoff
):
    """Undecodable accepted evidence cannot escape as an unclassified decode error."""
    storage, first = owned
    predecessor = await stop_between_parts(storage, first)
    parts = predecessor.progress.to_data()["parts"]
    parts[0]["handoff_id"] = (
        json.dumps(corrupt_handoff)
        if isinstance(corrupt_handoff, dict)
        else corrupt_handoff
    )
    storage._db.execute(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=? AND attempt_number=1",
        (json.dumps(parts), first.outbox_id),
    )
    storage._db.commit()
    retained = await storage.get_radio_attempt(first.outbox_id, 1)
    assert retained is not None
    current = await reserve(storage, first)
    native_calls = []

    async def unexpected_native_call(packet):
        native_calls.append(packet)
        raise AssertionError("invalid retained evidence reached native delivery")

    with pytest.raises(AdapterPermanentError, match="continuation evidence is invalid"):
        await deliver_radio_plan(
            storage, current, "next", unexpected_native_call, resume_from=retained
        )
    assert native_calls == []
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", "different-event"),
        ("delivery_plan_id", "different-plan"),
        ("target_adapter", "different-adapter"),
        ("target_channel", "9"),
    ],
)
async def test_historical_delivery_provenance_must_match_parent_identity(
    owned, field, value
):
    """A valid-looking checkpoint cannot inherit facts scoped to a different delivery."""
    storage, first = owned
    await stop_between_parts(storage, first)
    row = storage._db.execute(
        "SELECT provenance_json FROM radio_attempt_progress WHERE outbox_id=?",
        (first.outbox_id,),
    ).fetchone()
    provenance = json.loads(row[0])
    provenance[field] = value
    storage._db.execute(
        "UPDATE radio_attempt_progress SET provenance_json=? WHERE outbox_id=?",
        (json.dumps(provenance), first.outbox_id),
    )
    storage._db.commit()
    retained = await storage.get_radio_attempt(first.outbox_id, 1)
    current = await reserve(storage, first)
    with pytest.raises(StorageError, match="delivery identity"):
        await storage.resume_radio_attempt(
            current.attempt_provenance, "next", first.radio_plan, retained
        )
    assert await storage.get_radio_attempt(first.outbox_id, 2) is None
