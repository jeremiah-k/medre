"""Post-handoff delivery observation persistence and lifecycle tests."""

from __future__ import annotations

from datetime import datetime, timezone

from medre.core.contracts.adapter import OutboundDeliveryObservationRecord
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.events import CanonicalEvent, DeliveryObservation, EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.evidence._storage_sections import _collect_storage_data_from_backend
from medre.runtime.timeline import assemble_event_timeline


def _event(event_id: str = "evt-observation-1") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="source",
        source_transport_id="node",
        source_channel_id="chan",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(),
    )


async def _seed_attempt(
    storage,
    *,
    status: str = "in_progress",
    target_channel: str | None = "aa" * 16,
    target_address: str | None = None,
) -> DeliveryOutboxItem:
    event = _event()
    await storage.append(event)
    item = DeliveryOutboxItem(
        outbox_id="outbox-observation-1",
        event_id=event.event_id,
        route_id="route-observation",
        delivery_plan_id="plan-observation",
        target_adapter="lxmf-main",
        target_channel=target_channel,
        target_address=target_address,
        attempt_number=1,
        status="in_progress",
    )
    await storage.create_outbox_item(item)
    if status == "sent":
        await storage.mark_outbox_sent(item.outbox_id, attempt_number=1)
    elif status == "retry_wait":
        await storage.mark_outbox_retry_wait(
            item.outbox_id,
            datetime.now(timezone.utc).isoformat(),
            attempt_number=1,
        )
    return item


def _record(**overrides) -> OutboundDeliveryObservationRecord:
    values = {
        "event_id": "evt-observation-1",
        "adapter": "lxmf-main",
        "state": "delivered",
        "outbox_id": "outbox-observation-1",
        "attempt_number": 1,
        "delivery_plan_id": "plan-observation",
        "native_channel_id": "aa" * 16,
        "native_message_id": "bb" * 32,
        "metadata": {"lxmf": {"delivery_state": "delivered"}},
    }
    values.update(overrides)
    return OutboundDeliveryObservationRecord(**values)


async def test_observation_persists_idempotently_for_exact_sent_attempt(
    temp_storage,
) -> None:
    item = await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()
    now = datetime.now(timezone.utc)

    assert await lifecycle.record_delivery_observation(temp_storage, _record(), now)
    assert not await lifecycle.record_delivery_observation(temp_storage, _record(), now)

    observations = await temp_storage.list_delivery_observations_for_outbox(
        item.outbox_id
    )
    assert len(observations) == 1
    observation = observations[0]
    assert observation.event_id == item.event_id
    assert observation.delivery_plan_id == item.delivery_plan_id
    assert observation.attempt_number == 1
    assert observation.state == "delivered"
    assert observation.native_channel_id == "aa" * 16
    assert observation.adapter_message_id == "bb" * 32
    assert await temp_storage.count_delivery_observations() == 1


async def test_observation_can_arrive_while_handoff_attempt_is_in_progress(
    temp_storage,
) -> None:
    await _seed_attempt(temp_storage)
    lifecycle = DeliveryLifecycleService()

    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(state="failed", error="provider reported failure"),
        datetime.now(timezone.utc),
    )
    outbox = await temp_storage.get_outbox_item("outbox-observation-1")
    assert outbox is not None
    assert outbox.status == "in_progress"


async def test_failed_observation_does_not_reopen_sent_outbox(temp_storage) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()

    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(state="failed", error="provider later reported failure"),
        datetime.now(timezone.utc),
    )
    outbox = await temp_storage.get_outbox_item("outbox-observation-1")
    assert outbox is not None
    assert outbox.status == "sent"
    assert await temp_storage.count_delivery_observations() == 1


async def test_storage_append_atomically_rejects_attempt_after_state_changes(
    temp_storage,
) -> None:
    item = await _seed_attempt(temp_storage)
    observation = DeliveryObservation(
        observation_id="obs-atomic-stale",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        native_channel_id="aa" * 16,
        outbox_id=item.outbox_id,
        attempt_number=item.attempt_number,
        adapter_message_id="bb" * 32,
        state="delivered",
        confirmation_level="unknown",
        observed_at=datetime.now(timezone.utc),
    )

    await temp_storage.mark_outbox_retry_wait(
        item.outbox_id,
        datetime.now(timezone.utc).isoformat(),
        attempt_number=item.attempt_number,
    )

    assert not await temp_storage.append_delivery_observation(observation)
    assert await temp_storage.count_delivery_observations() == 0


async def test_observation_rejects_stale_retry_attempt_without_mutating_outbox(
    temp_storage,
) -> None:
    await _seed_attempt(temp_storage, status="retry_wait")
    lifecycle = DeliveryLifecycleService()

    assert not await lifecycle.record_delivery_observation(
        temp_storage, _record(), datetime.now(timezone.utc)
    )
    assert await temp_storage.count_delivery_observations() == 0
    outbox = await temp_storage.get_outbox_item("outbox-observation-1")
    assert outbox is not None
    assert outbox.status == "retry_wait"


async def test_observation_rejects_attempt_and_plan_mismatch(temp_storage) -> None:
    await _seed_attempt(temp_storage)
    lifecycle = DeliveryLifecycleService()
    now = datetime.now(timezone.utc)

    assert not await lifecycle.record_delivery_observation(
        temp_storage, _record(attempt_number=2), now
    )
    assert not await lifecycle.record_delivery_observation(
        temp_storage, _record(delivery_plan_id="wrong-plan"), now
    )
    assert await temp_storage.count_delivery_observations() == 0


async def test_native_channel_is_evidence_not_route_correlation(temp_storage) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()

    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(native_channel_id="cc" * 16),
        datetime.now(timezone.utc),
    )
    observations = await temp_storage.list_delivery_observations_for_outbox(
        "outbox-observation-1"
    )
    assert observations[0].target_channel == "aa" * 16
    assert observations[0].native_channel_id == "cc" * 16


async def test_structured_address_observation_does_not_require_target_channel(
    temp_storage,
) -> None:
    await _seed_attempt(
        temp_storage,
        status="sent",
        target_channel=None,
        target_address="lxmf:aa" + "aa" * 15,
    )
    lifecycle = DeliveryLifecycleService()

    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(native_channel_id="aa" * 16),
        datetime.now(timezone.utc),
    )
    observations = await temp_storage.list_delivery_observations_for_outbox(
        "outbox-observation-1"
    )
    assert observations[0].target_channel is None
    assert observations[0].native_channel_id == "aa" * 16


async def test_stronger_confirmation_is_distinct_append_only_observation(
    temp_storage,
) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()
    now = datetime.now(timezone.utc)

    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(confirmation_level="unknown"),
        now,
    )
    assert await lifecycle.record_delivery_observation(
        temp_storage,
        _record(confirmation_level="end_to_end"),
        now,
    )
    observations = await temp_storage.list_delivery_observations_for_outbox(
        "outbox-observation-1"
    )
    assert [o.confirmation_level for o in observations] == [
        "unknown",
        "end_to_end",
    ]


async def test_uncorrelated_observation_is_rejected(temp_storage) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()

    assert not await lifecycle.record_delivery_observation(
        temp_storage,
        _record(outbox_id=None),
        datetime.now(timezone.utc),
    )
    assert not await lifecycle.record_delivery_observation(
        temp_storage,
        _record(attempt_number=None),
        datetime.now(timezone.utc),
    )
    assert await temp_storage.count_delivery_observations() == 0


async def test_event_timeline_surfaces_post_handoff_observation(temp_storage) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()
    await lifecycle.record_delivery_observation(
        temp_storage, _record(), datetime.now(timezone.utc)
    )

    result = await assemble_event_timeline(temp_storage, "evt-observation-1")
    assert result is not None
    assert len(result["delivery_observations"]) == 1
    entries = [
        entry
        for entry in result["timeline_entries"]
        if entry["entry_type"] == "delivery_observation"
    ]
    assert len(entries) == 1
    assert entries[0]["data"]["state"] == "delivered"
    assert entries[0]["data"]["outbox_id"] == "outbox-observation-1"


async def test_storage_evidence_surfaces_observation_count_and_event_rows(
    temp_storage,
) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()
    await lifecycle.record_delivery_observation(
        temp_storage,
        _record(),
        datetime.now(timezone.utc),
    )

    section = await _collect_storage_data_from_backend(
        temp_storage,
        ":memory:",
        "evt-observation-1",
        None,
    )
    assert section["status"] == "passed"
    data = section["data"]
    assert data["delivery_observation_count"] == 1
    assert len(data["delivery_observations_for_event"]) == 1
    assert data["delivery_observations_for_event"][0]["state"] == "delivered"
    assert any(
        entry["entry_type"] == "delivery_observation" for entry in data["timeline"]
    )


async def test_observation_evidence_sanitizes_error_and_metadata(temp_storage) -> None:
    await _seed_attempt(temp_storage, status="sent")
    lifecycle = DeliveryLifecycleService()
    await lifecycle.record_delivery_observation(
        temp_storage,
        _record(
            state="failed",
            error="access_token=syt_supersecretvalue",
            metadata={
                "lxmf": {"delivery_state": "failed"},
                "access_token": "syt_should_not_escape",
            },
        ),
        datetime.now(timezone.utc),
    )

    result = await assemble_event_timeline(temp_storage, "evt-observation-1")
    assert result is not None
    entry = next(
        row
        for row in result["timeline_entries"]
        if row["entry_type"] == "delivery_observation"
    )
    assert "syt_supersecretvalue" not in entry["data"]["error"]
    assert "access_token" not in entry["data"]["metadata"]

    section = await _collect_storage_data_from_backend(
        temp_storage,
        ":memory:",
        "evt-observation-1",
        None,
    )
    observation = section["data"]["delivery_observations_for_event"][0]
    assert "syt_supersecretvalue" not in observation["error"]
    assert "access_token" not in observation["metadata"]


# ===================================================================
# Runner callback persistence re-try
# ===================================================================


class _FlakyLifecycle:
    """Lifecycle stand-in that fails the first N observation persists."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def record_delivery_observation(self, storage, record, now) -> bool:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("storage contention")
        return True


async def test_runner_record_delivery_observation_retries_once(temp_storage) -> None:
    """A transient persistence failure gets one immediate re-attempt."""
    from medre.core.engine.pipeline.runner import PipelineRunner
    from medre.core.routing import Router
    from tests.helpers.pipeline import make_pipeline_config_for_pipeline

    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(temp_storage, Router(routes=[]))
    )
    lifecycle = _FlakyLifecycle(failures=1)
    runner._lifecycle = lifecycle

    await runner._record_delivery_observation(_record())
    assert lifecycle.calls == 2


async def test_runner_record_delivery_observation_forfeits_after_retry(
    temp_storage, caplog
) -> None:
    """A callback whose re-attempt also fails is forfeited, not raised."""
    import logging

    from medre.core.engine.pipeline.runner import PipelineRunner
    from medre.core.routing import Router
    from tests.helpers.pipeline import make_pipeline_config_for_pipeline

    runner = PipelineRunner(
        make_pipeline_config_for_pipeline(temp_storage, Router(routes=[]))
    )
    lifecycle = _FlakyLifecycle(failures=99)
    runner._lifecycle = lifecycle

    with caplog.at_level(logging.ERROR):
        await runner._record_delivery_observation(_record())

    assert lifecycle.calls == 2
    assert any(
        "Failed to persist delivery observation" in record.message
        for record in caplog.records
    )
