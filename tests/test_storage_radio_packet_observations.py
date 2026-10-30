"""Delayed native packet ACKs require immutable original-attempt evidence."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.events import DeliveryObservation
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.recovery.radio_observations import (
    inspect_stored_radio_packet_observations,
    project_radio_packet_observations,
)
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.backend import StorageError
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import (
    create_outbox_item_with_parent,
    make_outbox_item,
)


@pytest.fixture
async def enrolled(tmp_path):
    """Retain a fully accepted multipart attempt under a live dispatch claim."""
    storage = SQLiteStorage(db_path=str(tmp_path / "radio-evidence.db"))
    await storage.initialize()
    try:
        item = replace(
            make_outbox_item(
                status="in_progress", target_adapter="meshtastic", target_channel="0"
            ),
            worker_id="pipeline:worker-one",
            lease_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        )
        item = await create_outbox_item_with_parent(storage, item)
        claim = DeliveryAttemptProvenance(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel,
            item.outbox_id,
            1,
            "live",
        )
        plan = plan_radio_text("café radio message " * 10, max_text_bytes=40)
        initial = await storage.bind_radio_attempt(claim, "pipeline:worker-one", plan)
        assert initial is not None
        current = initial
        for ordinal in range(1, len(plan.parts) + 1):
            started = await storage.begin_radio_part(
                current, "pipeline:worker-one", ordinal
            )
            assert started is not None
            current = await storage.observe_radio_part(
                started,
                "pipeline:worker-one",
                ordinal,
                handoff_id=RadioLocalHandoff(
                    ordinal, str(ordinal), "0", "local_transport"
                ).encode(),
            )
            assert current is not None
        yield storage, item, claim, current
    finally:
        await storage.close()


def observation(claim, *, ordinal=1, **overrides):
    """Build a remote packet fact scoped to its original accepting claim."""
    args = dict(
        observation_id=f"ack-{ordinal}",
        event_id=claim.event_id,
        delivery_plan_id=claim.delivery_plan_id,
        target_adapter=claim.target_adapter,
        target_channel=claim.target_channel,
        native_channel_id="0",
        outbox_id=claim.outbox_id,
        attempt_number=claim.attempt_number,
        adapter_message_id=str(ordinal),
        state="delivered",
        confirmation_level="remote_service",
        metadata={
            "radio_packet": {
                "ordinal": ordinal,
                "origin_outbox_id": claim.outbox_id,
                "origin_attempt_number": claim.attempt_number,
            }
        },
    )
    args.update(overrides)
    return DeliveryObservation(**args)


async def test_original_attempt_delayed_ack_survives_new_generation(enrolled):
    """Advancing the parent claim preserves delayed evidence and its original scope."""
    storage, item, claim, checkpoint = enrolled
    assert (
        await storage.reserve_outbox_attempt(item.outbox_id, "pipeline:worker-one", 1)
        == 2
    )
    first = observation(claim)
    assert await storage.append_radio_packet_observation(first)
    assert not await storage.append_radio_packet_observation(first)
    assert not await storage.append_delivery_observation(observation(claim, ordinal=2))
    history = await storage.list_delivery_observations_for_outbox(item.outbox_id)
    assert len(history) == 1
    assert history[0].attempt_number == 1
    assert project_radio_packet_observations(checkpoint, history).observed_parts == 1
    assert (await storage.get_outbox_item(item.outbox_id)).active_attempt == 2
    assert await storage.list_receipts_for_event(item.event_id) == []


@pytest.mark.parametrize(
    "update",
    [
        {"adapter_message_id": "other"},
        {"native_channel_id": "other"},
        {"target_channel": "wrong"},
        {"attempt_number": 2},
        {"attempt_number": True},
        {"attempt_number": 1.0},
        {"event_id": "other"},
        {"delivery_plan_id": "other"},
        {"target_adapter": "other"},
        {"confirmation_level": "local_transport"},
        {
            "metadata": {
                "radio_packet": {
                    "ordinal": 1,
                    "origin_outbox_id": "other",
                    "origin_attempt_number": 1,
                }
            }
        },
    ],
)
async def test_nonmatching_native_packet_cannot_enter_history(enrolled, update):
    """A near-matching callback cannot be normalized into accepted packet evidence."""
    storage, item, claim, _ = enrolled
    assert not await storage.append_radio_packet_observation(
        observation(claim, **update)
    )
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("bad_origin", [True, 1.0])
async def test_non_integer_origin_attempt_is_rejected(enrolled, bad_origin):
    """Native history cannot inherit an origin through numeric equality."""
    storage, item, claim, _ = enrolled
    bad = observation(
        claim,
        metadata={
            "radio_packet": {
                "ordinal": 1,
                "origin_outbox_id": claim.outbox_id,
                "origin_attempt_number": bad_origin,
            }
        },
    )
    assert not await storage.append_radio_packet_observation(bad)
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("ordinal", [True, 1.0])
async def test_non_integer_packet_ordinal_is_rejected(enrolled, ordinal):
    """Packet ordinals are exact integers, not numerically equal substitutes."""
    storage, item, claim, _ = enrolled
    bad = observation(
        claim,
        metadata={
            "radio_packet": {
                "ordinal": ordinal,
                "origin_outbox_id": claim.outbox_id,
                "origin_attempt_number": claim.attempt_number,
            }
        },
    )
    assert not await storage.append_radio_packet_observation(bad)
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("ordinal", [0, -1, 999])
async def test_out_of_range_ordinal_cannot_enter_history(enrolled, ordinal):
    """Correct parent correlation must not bypass the packet range check."""
    storage, item, claim, _ = enrolled
    bad = observation(
        claim,
        ordinal=ordinal,
        metadata={
            "radio_packet": {
                "ordinal": ordinal,
                "origin_outbox_id": claim.outbox_id,
                "origin_attempt_number": claim.attempt_number,
            }
        },
    )
    assert not await storage.append_radio_packet_observation(bad)
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("malformed", ["not-json", "null", "[]", '{"ordinal":"1"}'])
async def test_corrupt_original_handoff_is_never_accepted(enrolled, malformed):
    """An undecodable retained acceptance prevents admission without adding history."""
    storage, item, claim, _ = enrolled

    snapshot = await storage.get_radio_attempt_snapshot(item.outbox_id, 1)
    assert snapshot is not None
    # Simulate a corrupted persisted record to exercise the closed decoder.
    db = storage._db
    parts = json.loads(
        db.execute(
            "SELECT parts_json FROM radio_attempt_progress WHERE outbox_id=? AND attempt_number=1",
            (item.outbox_id,),
        ).fetchone()[0]
    )
    parts[0]["handoff_id"] = malformed
    db.execute(
        "UPDATE radio_attempt_progress SET parts_json=? WHERE outbox_id=? AND attempt_number=1",
        (json.dumps(parts), item.outbox_id),
    )
    db.commit()
    with pytest.raises(
        StorageError, match="Retained radio attempt evidence is invalid"
    ):
        await storage.append_radio_packet_observation(observation(claim))
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("state", ["unsent", "submitting", "unknown"])
async def test_unaccepted_packet_cannot_acquire_native_observation(enrolled, state):
    """An ACK cannot manufacture acceptance for a retained uncertain packet."""
    storage, item, claim, cp = enrolled
    # Replace the checkpoint with a valid prefix followed by unaccepted parts.
    parts = [
        {"ordinal": part.ordinal, "state": "unsent", "handoff_id": None}
        for part in cp.progress.parts
    ]
    parts[0]["state"] = state
    storage._db.execute(
        "UPDATE radio_attempt_progress SET parts_json=?, revision=? WHERE outbox_id=?",
        (
            json.dumps(parts),
            {"unsent": 0, "submitting": 1, "unknown": 2}[state],
            item.outbox_id,
        ),
    )
    storage._db.commit()
    assert not await storage.append_radio_packet_observation(observation(claim))
    assert await storage.list_delivery_observations_for_outbox(item.outbox_id) == []


@pytest.mark.parametrize("state", ["failed", "rejected", "cancelled"])
async def test_negative_native_evidence_is_retained_without_parent_transition(
    enrolled, state
):
    """Conflicting native reports remain auditable and hold the read-only summary."""
    storage, item, claim, cp = enrolled
    assert await storage.append_radio_packet_observation(observation(claim))
    assert await storage.append_radio_packet_observation(
        observation(claim, observation_id="negative", state=state)
    )
    summary = await inspect_stored_radio_packet_observations(storage, item.outbox_id, 1)
    assert summary is not None and summary.status == "held"
    assert summary.reason == "conflicting_or_negative_packet_observation"
    assert await storage.get_radio_attempt(item.outbox_id, 1) == cp
    assert (await storage.get_outbox_item(item.outbox_id)).status == "in_progress"
    assert await storage.list_receipts_for_event(item.event_id) == []


async def test_storage_bound_projection_never_advances_parent_delivery(enrolled):
    """Whole-plan packet observations remain independent from receipt finalization."""
    storage, item, claim, cp = enrolled
    for ordinal in range(1, len(cp.progress.parts) + 1):
        assert await storage.append_radio_packet_observation(
            observation(claim, ordinal=ordinal)
        )
    summary = await inspect_stored_radio_packet_observations(storage, item.outbox_id, 1)
    assert summary is not None
    assert summary.status == "all_packets_observed"
    assert summary.native_submission_authorized is False
    assert (await storage.get_outbox_item(item.outbox_id)).status == "in_progress"
    assert await storage.list_receipts_for_event(item.event_id) == []
    assert (
        await inspect_stored_radio_packet_observations(storage, item.outbox_id, 999)
        is None
    )
