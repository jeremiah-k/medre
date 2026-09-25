"""Factories for exact asynchronous delivery callback provenance."""

from __future__ import annotations

from typing import Literal

from medre.core.contracts.delivery import DeferredHandoffFailed
from medre.core.events import DeliveryAttemptProvenance, DeliverySource


def make_deferred_failure(
    *,
    event_id: str,
    adapter: str,
    outcome: Literal["exhausted", "permanent_failed", "cancelled", "abandoned"],
    outbox_id: str | None = None,
    delivery_plan_id: str | None = None,
    attempt_number: int = 1,
    native_channel_id: str | None = None,
    error: str | None = None,
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
    provenance_plan_id: str | None = None,
    provenance_channel: str | None = None,
    attempt_provenance: DeliveryAttemptProvenance | None = None,
) -> DeferredHandoffFailed:
    if attempt_provenance is None and outbox_id is None:
        raise TypeError("attempt_provenance or outbox_id is required")
    if attempt_provenance is None:
        assert outbox_id is not None
    provenance = attempt_provenance or DeliveryAttemptProvenance(
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
    return DeferredHandoffFailed(
        attempt_provenance=provenance,
        outcome=outcome,
        native_channel_id=native_channel_id,
        error=error,
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


def make_deferred_completion(
    *,
    event_id: str,
    adapter: str,
    native_message_id: str | None,
    outbox_id: str | None = None,
    delivery_plan_id: str | None = None,
    attempt_number: int = 1,
    native_channel_id: str | None = None,
    native_thread_id: str | None = None,
    native_relation_id: str | None = None,
    metadata: dict[str, object] | None = None,
    confirmation_level: str = "local_transport",
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
    provenance_channel: str | None = None,
    attempt_provenance: DeliveryAttemptProvenance | None = None,
):
    """Build one exact deferred-completion feedback fact for tests."""
    from medre.core.contracts.delivery import (
        AdapterHandoffResult,
        DeferredHandoffCompleted,
    )

    if attempt_provenance is None and outbox_id is None:
        raise TypeError("outbox_id is required when attempt_provenance is not supplied")
    if attempt_provenance is None:
        assert outbox_id is not None
    provenance = attempt_provenance or DeliveryAttemptProvenance(
        event_id=event_id,
        delivery_plan_id=delivery_plan_id or "plan-1",
        target_adapter=adapter,
        target_channel=(
            provenance_channel if provenance_channel is not None else native_channel_id
        ),
        outbox_id=outbox_id,
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
    )
    return DeferredHandoffCompleted(
        attempt_provenance=provenance,
        handoff=AdapterHandoffResult(
            native_message_id=native_message_id,
            native_channel_id=native_channel_id,
            native_thread_id=native_thread_id,
            native_relation_id=native_relation_id,
            confirmation_level=confirmation_level,  # type: ignore[arg-type]
            metadata=metadata or {},
        ),
    )


def make_post_handoff_observation(
    *,
    event_id: str,
    adapter: str,
    state: str,
    outbox_id: str | None = None,
    delivery_plan_id: str | None = None,
    attempt_number: int = 1,
    native_channel_id: str | None = None,
    native_message_id: str | None = None,
    confirmation_level: str = "unknown",
    error: str | None = None,
    metadata: dict[str, object] | None = None,
    source: DeliverySource = "live",
    replay_run_id: str | None = None,
    provenance_channel: str | None = None,
    attempt_provenance: DeliveryAttemptProvenance | None = None,
):
    """Build one exact post-handoff observation fact for tests."""
    from medre.core.contracts.delivery import PostHandoffObservation

    if attempt_provenance is None and outbox_id is None:
        raise TypeError("outbox_id is required when attempt_provenance is not supplied")
    if attempt_provenance is None:
        assert outbox_id is not None
    provenance = attempt_provenance or DeliveryAttemptProvenance(
        event_id=event_id,
        delivery_plan_id=delivery_plan_id or "plan-1",
        target_adapter=adapter,
        target_channel=(
            provenance_channel if provenance_channel is not None else native_channel_id
        ),
        outbox_id=outbox_id or "",
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
    )
    return PostHandoffObservation(
        attempt_provenance=provenance,
        state=state,  # type: ignore[arg-type]
        native_channel_id=native_channel_id,
        native_message_id=native_message_id,
        confirmation_level=confirmation_level,  # type: ignore[arg-type]
        error=error,
        metadata=metadata or {},
    )
