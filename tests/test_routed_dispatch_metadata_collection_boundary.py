"""Production outbox snapshots block collection while their work is live.

The real manager freezes route/capability context in the owned work snapshot
table. A pending snapshot may contain free-form destination metadata or
origin labels, and its unretired presence blocks release as live work state.
Terminal settlement retires the snapshot inside the guarded transition; the
settled ordinary text event carries no outbox content blocker, and a preview
reports eligibility without deleting anything. Historical rows created before
the snapshot table keep their inline metadata bytes and stay opaque blockers.
"""

from __future__ import annotations

import msgspec

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.events import EventMetadata
from medre.core.events.metadata import RoutingMetadata
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_production_dispatch_context_blocks_until_classified(tmp_path):
    store = SQLiteStorage(str(tmp_path / "dispatch-metadata.db"))
    await store.initialize()
    try:
        metadata = msgspec.structs.replace(
            EventMetadata(), routing=RoutingMetadata(matched_routes=("route-1",))
        )
        event = msgspec.structs.replace(
            make_storage_event(event_id="staged-context", payload={"body": "secret"}),
            metadata=metadata,
        )
        await store.admit_ingress(event, None, "live")
        target = RouteTarget(adapter="fake_presentation", channel="destination")
        route = Route(
            id="route-1",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[target],
        )
        plan = DeliveryPlan(
            plan_id="plan-1",
            event_id=event.event_id,
            route_id=route.id,
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
            capability_level="native",
        )
        context = await OutboxManager(
            store, DeliveryLifecycleService()
        ).create_for_delivery(
            event, route, plan, target, "fake_presentation", pending=True
        )
        assert context.outbox_id is not None
        stored = await store.get_outbox_item(context.outbox_id)
        assert stored is not None
        # The frozen production context is privately owned snapshot bytes,
        # never inline outbox metadata; its unretired presence is the blocker.
        assert stored.metadata == {}
        claims = await store.claim_ingress_work(worker_id="worker")
        assert [claim.event_id for claim in claims] == [event.event_id]
        assert await store.complete_ingress_work(event.event_id, worker_id="worker")
        before = await store.collect_event_content(event.event_id)
        assert "unretired_outbox_snapshots" in before["blockers"]
        # Terminal settlement retires the snapshot inside the guarded
        # transition; the settled event has no outbox content blocker left
        # and a preview reports eligibility without deleting anything.
        claimed = await store.claim_due_outbox_items(
            "2026-10-31T00:00:00+00:00", "delivery"
        )
        assert [item.outbox_id for item in claimed] == [context.outbox_id]
        assert await store.mark_outbox_sent(context.outbox_id)
        retired = await store.get_outbox_dispatch_context(
            context.outbox_id,
            event_id=stored.event_id,
            delivery_plan_id=stored.delivery_plan_id,
            target_adapter=stored.target_adapter,
            target_channel=stored.target_channel,
        )
        assert retired is None
        after = await store.collect_event_content(event.event_id)
        assert after["status"] != "blocked"
        assert "unretired_outbox_snapshots" not in after["blockers"]
        assert "outbox_records" not in after["blockers"]
        retained = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id=?", (event.event_id,)
        )
        assert retained is not None and retained["body"] == "secret"
    finally:
        await store.close()
