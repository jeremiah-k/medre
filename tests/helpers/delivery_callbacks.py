"""Factories for exact asynchronous delivery callback provenance."""

from __future__ import annotations

from typing import Literal

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.events import DeliveryAttemptProvenance, DeliverySource


def make_terminal_record(
    *,
    event_id: str,
    adapter: str,
    outcome: Literal["exhausted", "permanent_failed", "cancelled", "abandoned"],
    outbox_id: str | None,
    delivery_plan_id: str | None = None,
    attempt_number: int | None = 1,
    native_channel_id: str | None = None,
    error: str | None = None,
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
    provenance_plan_id: str | None = None,
    provenance_channel: str | None = None,
    with_provenance: bool = True,
) -> QueueTerminalRecord:
    provenance = None
    if with_provenance and outbox_id is not None and attempt_number is not None:
        provenance = DeliveryAttemptProvenance(
            event_id=event_id,
            delivery_plan_id=provenance_plan_id or delivery_plan_id or "plan-1",
            target_adapter=adapter,
            target_channel=(
                provenance_channel
                if provenance_channel is not None
                else native_channel_id if native_channel_id is not None else "0"
            ),
            outbox_id=outbox_id,
            attempt_number=attempt_number,
            source=source,
            replay_run_id=replay_run_id,
        )
    return QueueTerminalRecord(
        event_id=event_id,
        adapter=adapter,
        outcome=outcome,
        outbox_id=outbox_id,
        delivery_plan_id=delivery_plan_id,
        attempt_number=attempt_number,
        native_channel_id=native_channel_id,
        error=error,
        attempt_provenance=provenance,
    )


def make_attempt_provenance(
    *,
    event_id: str,
    target_adapter: str,
    outbox_id: str,
    attempt_number: int,
    delivery_plan_id: str | None = None,
    target_channel: str | None = None,
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
) -> DeliveryAttemptProvenance:
    """Build a hand-off envelope from the same facts as the callback record."""
    return DeliveryAttemptProvenance(
        event_id=event_id,
        delivery_plan_id=delivery_plan_id or "plan-1",
        target_adapter=target_adapter,
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        target_channel=target_channel,
        source=source,
        replay_run_id=replay_run_id,
    )
