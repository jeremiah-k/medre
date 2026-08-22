"""Runtime regression coverage for retry-plan reconstruction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.events.canonical import DeliveryReceipt
from medre.core.planning.delivery_plan import DeliveryStrategy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.retry_runtime import (
    build_retry_runner,
    make_retry_event,
    start_retry_adapters,
)


async def test_reconstructed_retry_plan_carries_metadata_through_worker(
    temp_storage: SQLiteStorage,
) -> None:
    """Regression: RetryWorker's retry execution receives reconstructed
    route/plan/destination from persisted outbox+receipt data.

    Proves the full chain:
      outbox.metadata (destination_kind/hash/name/metadata)
        + receipt.retry_* fields
        -> reconstruct_retry_delivery_plan()
        -> pipeline.deliver_to_target(route=..., plan=...)
    """
    from medre.config.model import RuntimeLimits
    from medre.core.supervision.capacity import CapacityController
    from medre.runtime.retry import RetryWorker

    # -- Constants for the seeded data --------------------------------
    event_id = "recon-evt-001"
    route_id = "recon-route-42"
    plan_id = "recon-plan-abc"
    adapter_id = "recon_target"
    channel = "#recon-room"

    dest_metadata = {"room_id": "!abc:server", "via": ["server.org"]}

    # -- 1. Seed a canonical event into storage -----------------------
    event = make_retry_event(event_id=event_id)
    await temp_storage.append(event)

    # -- 2. Seed a previous failed receipt with retry policy ----------
    receipt = DeliveryReceipt(
        sequence=1,
        receipt_id="recon-rcpt-001",
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter_id,
        target_channel=channel,
        route_id=route_id,
        status="failed",
        error="ConnectionError: transient",
        failure_kind="adapter_transient",
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=10),
        attempt_number=1,
        retry_max_attempts=5,
        retry_backoff_base=3.0,
        retry_max_delay=90.0,
        retry_jitter=True,
        created_at=datetime.now(timezone.utc),
    )
    await temp_storage.append_receipt(receipt)

    # -- 3. Seed an outbox item with destination metadata -------------
    outbox_item = DeliveryOutboxItem(
        outbox_id=f"recon-ob-{uuid.uuid4().hex[:8]}",
        event_id=event_id,
        route_id=route_id,
        delivery_plan_id=plan_id,
        target_adapter=adapter_id,
        target_channel=channel,
        attempt_number=2,
        status="in_progress",
        metadata={
            "destination_kind": "matrix_room",
            "destination_hash": "deadbeef",
            "destination_name": "test-room",
            "destination_metadata": dest_metadata,
            "capability_level": None,
            "delivery_strategy": "direct",
            "capability_field": None,
            "capability_reason": None,
            "deadline": None,
        },
    )
    await temp_storage.create_outbox_item(outbox_item)

    # -- 4. Build a pipeline and intercept deliver_to_target ----------
    adapter = FakePresentationAdapter(adapter_id=adapter_id)
    route = Route(
        id=route_id,
        source=RouteSource(
            adapter="fake_source",
            event_kinds=("message.created",),
            channel=None,
        ),
        targets=[RouteTarget(adapter=adapter_id, channel=channel)],
    )
    router = Router(routes=[route])
    accounting = RuntimeAccounting()
    adapters = {adapter_id: adapter}

    runner = build_retry_runner(temp_storage, adapters, router, accounting)
    await start_retry_adapters(adapters)
    await runner.start()

    try:
        # Capture what deliver_to_target receives
        captured: dict = {}

        async def capturing_deliver(event, route, plan, **kwargs):
            captured["event"] = event
            captured["route"] = route
            captured["plan"] = plan
            captured["kwargs"] = kwargs
            # Return a fake "sent" receipt so the worker succeeds
            return DeliveryReceipt(
                sequence=2,
                receipt_id=f"recon-rcpt-{uuid.uuid4().hex[:8]}",
                event_id=event_id,
                delivery_plan_id=plan_id,
                target_adapter=adapter_id,
                target_channel=channel,
                route_id=route_id,
                status="sent",
                attempt_number=2,
                parent_receipt_id=receipt.receipt_id,
                source="retry",
                created_at=datetime.now(timezone.utc),
            )

        # Intercept the pipeline boundary to assert reconstructed Route/DeliveryPlan
        # passed by RetryWorker without invoking adapter delivery/rendering.
        runner.deliver_to_target = capturing_deliver

        # -- 5. Instantiate real RetryWorker and process the item ------
        limits = RuntimeLimits(
            max_inflight_deliveries=10,
            max_inflight_replay_events=10,
            shutdown_drain_timeout_seconds=5,
            delivery_acquire_timeout_seconds=0.5,
        )
        capacity = CapacityController(limits)
        # max_attempts=3 is DIFFERENT from the stored 5 — proves
        # the receipt value wins over the worker default.
        worker = RetryWorker(
            temp_storage,
            runner,
            capacity,
            enabled=True,
            interval_seconds=10.0,
            batch_size=20,
            max_attempts=3,
        )

        await worker._retry_outbox_item(outbox_item)

        # -- 6. Assert reconstruction carried correct metadata ---------
        assert captured, "deliver_to_target was never called"

        # Route identity
        cap_route = captured["route"]
        assert cap_route.id == route_id

        # Plan identity
        cap_plan = captured["plan"]
        assert cap_plan.plan_id == plan_id
        assert cap_plan.event_id == event_id
        assert cap_plan.route_id == route_id

        # Target adapter and channel
        assert cap_plan.target.adapter == adapter_id
        assert cap_plan.target.channel == channel

        # Reconstructed destination from outbox metadata
        dest = cap_plan.target.destination
        assert dest is not None
        assert dest.kind == "matrix_room"
        assert dest.destination_hash == "deadbeef"
        assert dest.destination_name == "test-room"
        assert dest.metadata == dest_metadata

        # Retry policy reconstructed from the receipt (not worker default)
        policy = cap_plan.retry_policy
        assert policy.max_attempts == 5  # from receipt, not worker's 3
        assert policy.backoff_base == 3.0
        assert policy.max_delay_seconds == 90.0
        assert policy.jitter is True

        # Capability level reconstructed from persisted metadata (seeded None)
        assert cap_plan.capability_level is None

        # Strategy reconstructed from persisted delivery_strategy metadata
        assert cap_plan.primary_strategy == DeliveryStrategy(method="direct")

        # previous_receipt was passed through (read from storage, so
        # check value equality rather than object identity)
        prev_rcpt = captured["kwargs"]["previous_receipt"]
        assert prev_rcpt is not None
        assert prev_rcpt.receipt_id == receipt.receipt_id
        assert captured["kwargs"]["source"] == "retry"

        # Worker state reflects success
        assert worker.state.succeeded == 1
        assert worker.state.processed == 1
    finally:
        await runner.stop()
