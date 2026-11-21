"""Shared exact-attempt evidence and real SQLite reservation fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.events import DeliveryObservation
from medre.core.events.delivery import DeliveryAttemptProvenance
from medre.core.rendering.radio_progress import RadioAttemptProgress, RadioPartProgress
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.radio import StoredRadioAttempt
from medre.core.storage.radio_handoff import RadioLocalHandoff
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import create_outbox_item_with_parent

_WORKER = "reconcile-worker-test"

_PLAN_ID = "plan-handoff-resolution"


def _past() -> datetime:
    """A lapsed timestamp computed at test execution time, never at import."""
    return datetime.now(timezone.utc) - timedelta(seconds=120)


def _lease_end() -> str:
    """A fresh claim lease computed at test execution time.

    Module-import timestamps expire before a slow suite reaches these tests,
    and the guarded radio bind refuses an expired claim fence, so every
    seeded lease must be generated when the test actually runs.
    """
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


def _plan():
    """A real multibyte, prefixed plan with exactly three ordered packets."""
    return plan_radio_text("café α " * 6, max_text_bytes=40, prefix="[North] ")


def _identity(
    event_id: str = "evt-handoff-resolution",
    adapter: str = "radio",
    channel: str | None = "ch-0",
) -> DeliveryIdentity:
    return DeliveryIdentity(event_id, _PLAN_ID, adapter, channel)


def _provenance(
    outbox_id: str,
    attempt_number: int,
    *,
    event_id: str = "evt-handoff-resolution",
    adapter: str = "radio",
    channel: str | None = "ch-0",
    source: str = "live",
    replay_run_id: str | None = None,
) -> DeliveryAttemptProvenance:
    return DeliveryAttemptProvenance(
        event_id=event_id,
        delivery_plan_id=_PLAN_ID,
        target_adapter=adapter,
        target_channel=channel,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
    )


def _checkpoint(
    provenance: DeliveryAttemptProvenance,
    states: list[str],
) -> StoredRadioAttempt:
    """Build a stored radio checkpoint with the requested part states.

    States must satisfy the accepted-prefix invariant: accepted parts carry a
    handoff reference, and the first non-accepted part owns the boundary.
    """
    plan = _plan()
    assert len(states) == len(plan.parts)
    parts = []
    for ordinal, state in enumerate(states, 1):
        if state == "accepted":
            parts.append(
                RadioPartProgress(
                    ordinal,
                    "accepted",
                    RadioLocalHandoff(
                        ordinal, f"native-{ordinal}", "ch-0", "local_transport"
                    ).encode(),
                )
            )
        else:
            parts.append(RadioPartProgress(ordinal, state))
    progress = RadioAttemptProgress(
        provenance.outbox_id, provenance.attempt_number, plan, tuple(parts)
    )
    revision = sum(
        0 if part.state == "unsent" else 1 if part.state == "submitting" else 2
        for part in progress.parts
    )
    return StoredRadioAttempt(provenance, progress, revision)


def _observation(
    observation_id: str,
    provenance: DeliveryAttemptProvenance,
    *,
    state: str = "delivered",
    confirmation_level: str = "end_to_end",
    radio_packet: object = None,
    native_channel_id: str | None = "ch-0",
    adapter_message_id: str | None = "native-1",
) -> DeliveryObservation:
    metadata: dict[str, object] = {}
    if radio_packet is not None:
        metadata["radio_packet"] = radio_packet
    return DeliveryObservation(
        observation_id=observation_id,
        event_id=provenance.event_id,
        delivery_plan_id=provenance.delivery_plan_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        native_channel_id=native_channel_id,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        adapter_message_id=adapter_message_id,
        state=state,  # type: ignore[arg-type]
        confirmation_level=confirmation_level,  # type: ignore[arg-type]
        metadata=metadata,
        observed_at=datetime.now(timezone.utc),
    )


async def _seed_reserved_claim(
    storage: SQLiteStorage,
    *,
    outbox_id: str = "obox-reconcile",
    event_id: str = "evt-handoff-resolution",
    adapter: str = "radio",
    channel: str | None = "ch-0",
    worker: str = _WORKER,
    replay_run_id: str | None = None,
    status: str = "in_progress",
    attempt_number: int = 0,
) -> tuple[DeliveryOutboxItem, DeliveryAttemptProvenance]:
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-handoff-resolution",
        delivery_plan_id=_PLAN_ID,
        target_adapter=adapter,
        target_channel=channel,
        attempt_number=attempt_number,
        status=status,
        locked_at=_past().isoformat(),
        lease_until=_lease_end(),
        worker_id=worker,
        dispatch_source="replay" if replay_run_id else "live",
        replay_run_id=replay_run_id,
        metadata={
            "capability_level": None,
            "delivery_strategy": "direct",
            "capability_field": None,
            "capability_reason": None,
            "deadline": None,
        },
    )
    stored = await create_outbox_item_with_parent(
        storage, item, allocate_new_generation=replay_run_id is not None
    )
    reserved = await storage.reserve_outbox_attempt(
        outbox_id, worker, stored.attempt_number
    )
    assert reserved == stored.attempt_number + 1
    # The dispatch reservation stamps the durable provenance the original
    # dispatch ran under (a named replay dispatched by the retry worker
    # carries source ``retry`` with its run lineage).
    reserved_row = await storage.get_outbox_item(outbox_id)
    assert reserved_row is not None
    # This helper reserves through the retry lane, including attempt zero.
    source = "retry"
    provenance = _provenance(
        outbox_id,
        reserved,
        event_id=event_id,
        adapter=adapter,
        channel=channel,
        source=source,
        replay_run_id=replay_run_id,
    )
    return stored, provenance


async def _bind_and_progress(
    storage: SQLiteStorage,
    provenance: DeliveryAttemptProvenance,
    states: list[str],
    worker: str = _WORKER,
    *,
    payload_fingerprint: str | None = None,
) -> None:
    """Drive the real guarded transitions through the requested states."""
    plan = _plan()
    checkpoint = await storage.bind_radio_attempt(provenance, worker, plan)
    assert checkpoint is not None
    for ordinal, state in enumerate(states, 1):
        if state == "unsent":
            continue
        submitting = await storage.begin_radio_part(checkpoint, worker, ordinal)
        assert submitting is not None
        if state == "accepted":
            observed = await storage.observe_radio_part(
                submitting,
                worker,
                ordinal,
                handoff_id=RadioLocalHandoff(
                    ordinal,
                    f"native-{ordinal}",
                    "ch-0",
                    "local_transport",
                    payload_fingerprint=payload_fingerprint,
                ).encode(),
            )
            assert observed is not None
            checkpoint = observed
        elif state == "unknown":
            observed = await storage.observe_radio_part(submitting, worker, ordinal)
            assert observed is not None
            checkpoint = observed
        # "submitting" stays in flight.


async def _claimed_row(storage: SQLiteStorage, outbox_id: str, worker: str):
    # Test setup only: let the seeded owner's lease lapse so the poll reclaims.
    await storage._write_rowcount(
        "UPDATE delivery_outbox SET lease_until = ? WHERE outbox_id = ?",
        (_past().isoformat(), outbox_id),
    )
    due = datetime.now(timezone.utc).isoformat()
    claimed = [
        row
        for row in await storage.claim_due_outbox_items(now=due, worker_id=worker)
        if row.outbox_id == outbox_id
    ]
    assert claimed
    return claimed[0]


async def _due_row(storage: SQLiteStorage, outbox_id: str, worker: str):
    # Test setup only: make a retry_wait row due (lapsed lease, past schedule)
    # so a fresh poll reclaims it for the pre-dispatch recheck.
    await storage._write_rowcount(
        "UPDATE delivery_outbox SET lease_until = ?, next_attempt_at = ? "
        "WHERE outbox_id = ?",
        (_past().isoformat(), _past().isoformat(), outbox_id),
    )
    due = datetime.now(timezone.utc).isoformat()
    claimed = [
        row
        for row in await storage.claim_due_outbox_items(now=due, worker_id=worker)
        if row.outbox_id == outbox_id
    ]
    assert claimed
    return claimed[0]


async def _row(storage: SQLiteStorage, outbox_id: str) -> DeliveryOutboxItem:
    row = await storage.get_outbox_item(outbox_id)
    assert row is not None
    return row
