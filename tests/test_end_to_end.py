"""End-to-end integration test: full pipeline from fake transport inbound
through storage, routing, delivery planning, fake presentation delivery,
native ref storage, and reply resolution.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from medre.adapters import (
    AdapterRole,
    FakePresentationAdapter,
    FakeTransportAdapter,
)
from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.planning import (
    DeliveryPlan,
    DeliveryStrategy,
    FallbackResolver,
    RelationResolver,
)
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage import EventFilter, SQLiteStorage


# ---------------------------------------------------------------------------
# Helper: adapter-wrapping storage for RelationResolver
# ---------------------------------------------------------------------------


class _StorageAdapterForResolver:
    """Wraps SQLiteStorage so that resolve_native_ref accepts split fields
    (adapter, channel, message_id) and returns an event_id string (or None),
    matching the RelationResolver storage contract.
    """

    def __init__(self, storage: SQLiteStorage) -> None:
        self._storage = storage

    async def resolve_native_ref(
        self, adapter: str, channel: str, message_id: str
    ) -> str | None:
        return await self._storage.resolve_native_ref(adapter, channel, message_id)


# ===================================================================
# Full pipeline test
# ===================================================================


class TestFullPipeline:
    """End-to-end: inbound → storage → route → plan → deliver → ref → reply."""

    async def test_complete_pipeline(self) -> None:
        """Full event lifecycle through every subsystem."""
        # ------------------------------------------------------------------
        # 1. Create storage, router, adapters
        # ------------------------------------------------------------------
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            route = Route(
                id="e2e-route",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created", "message.text"),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter="fake_presentation")],
            )
            router = Router(routes=[route])
            fallback_resolver = FallbackResolver()

            transport = FakeTransportAdapter("fake_transport", channel="ch-0")
            presentation = FakePresentationAdapter("fake_presentation")

            # Track inbound events published by adapters.
            inbound_events: list[CanonicalEvent] = []

            async def publish_inbound(event: CanonicalEvent) -> None:
                inbound_events.append(event)

            # Create adapter contexts.
            import logging

            transport_ctx = type(transport).start.__code__  # just for reference
            from medre.adapters.base import AdapterContext
            import asyncio

            t_ctx = AdapterContext(
                adapter_id="fake_transport",
                event_bus=None,
                publish_inbound=publish_inbound,
                logger=logging.getLogger("e2e.transport"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )
            p_ctx = AdapterContext(
                adapter_id="fake_presentation",
                event_bus=None,
                publish_inbound=publish_inbound,
                logger=logging.getLogger("e2e.presentation"),
                clock=lambda: datetime.now(timezone.utc),
                shutdown_event=asyncio.Event(),
            )

            await transport.start(t_ctx)
            await presentation.start(p_ctx)

            # ------------------------------------------------------------------
            # 2. Fake transport simulates inbound message
            # ------------------------------------------------------------------
            event = transport.make_event(
                text="Hello from radio",
                event_kind="message.created",
            )
            await transport.simulate_inbound(event)
            assert event in inbound_events, "Event should be published inbound"

            # ------------------------------------------------------------------
            # 3. Event is stored in canonical_events
            # ------------------------------------------------------------------
            await storage.append(event)
            stored = await storage.get(event.event_id)
            assert stored is not None
            assert stored.event_id == event.event_id
            assert stored.payload["body"] == "Hello from radio"

            # ------------------------------------------------------------------
            # 4. Router matches route
            # ------------------------------------------------------------------
            matched = router.match(event)
            assert len(matched) == 1
            assert matched[0].id == "e2e-route"

            # ------------------------------------------------------------------
            # 5. Delivery plan is created
            # ------------------------------------------------------------------
            matched_route = matched[0]
            targets = router.resolve_targets(event, matched_route)
            assert len(targets) == 1
            target = targets[0]

            plan = fallback_resolver.resolve_fallback(
                event, target, capabilities={}
            )
            assert isinstance(plan, DeliveryPlan)
            assert plan.event_id == event.event_id
            assert plan.primary_strategy.method == "direct"

            # ------------------------------------------------------------------
            # 6. Fake presentation receives event (deliver)
            # ------------------------------------------------------------------
            from medre.core.rendering.renderer import RenderingResult

            render_result = RenderingResult(
                event_id=event.event_id,
                target_adapter="fake_presentation",
                target_channel="ch-0",
                payload={"text": "Hello from radio"},
            )
            delivery_result = await presentation.deliver(render_result)
            assert len(presentation.delivered_payloads) == 1

            # ------------------------------------------------------------------
            # 7. Native ref is stored using adapter-provided native IDs
            # ------------------------------------------------------------------
            # The fake presentation adapter returns deterministic native IDs.
            assert delivery_result is not None
            assert delivery_result.native_message_id is not None
            native_msg_id = delivery_result.native_message_id

            native_ref_mapping = NativeMessageRef(
                id=f"nref-{event.event_id}",
                event_id=event.event_id,
                adapter="fake_presentation",
                native_channel_id="ch-0",
                native_message_id=native_msg_id,
                native_thread_id=None,
                native_relation_id=None,
                direction="outbound",
                metadata={},
            )
            await storage.store_native_ref(native_ref_mapping)

            # Verify it can be resolved back.
            resolved_id = await storage.resolve_native_ref(
                "fake_presentation", "ch-0", native_msg_id
            )
            assert resolved_id == event.event_id

            # ------------------------------------------------------------------
            # 8. Reply relation resolves via native ref
            # ------------------------------------------------------------------
            # Simulate a reply event from the presentation adapter.
            reply_event = presentation.make_reply_event(
                target=event, text="Reply from chat"
            )
            assert len(reply_event.relations) == 1
            reply_relation = reply_event.relations[0]
            assert reply_relation.relation_type == "reply"
            assert reply_relation.target_event_id == event.event_id

            # Store the reply and its native ref.
            await storage.append(reply_event)
            # Use adapter-provided native IDs for reply.
            reply_native_id = f"fake-pres-{reply_event.event_id}"
            reply_native_ref = NativeMessageRef(
                id=f"nref-{reply_event.event_id}",
                event_id=reply_event.event_id,
                adapter="fake_presentation",
                native_channel_id="ch-0",
                native_message_id=reply_native_id,
                native_thread_id=None,
                native_relation_id=native_msg_id,
                direction="inbound",
            )
            await storage.store_native_ref(reply_native_ref)

            # Use RelationResolver with wrapped storage to resolve a
            # relation that only has a native ref (no canonical ID).
            wrapped_storage = _StorageAdapterForResolver(storage)
            resolver = RelationResolver(storage=wrapped_storage)

            unresolved_relation = EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=NativeRef(
                    adapter="fake_presentation",
                    native_channel_id="ch-0",
                    native_message_id=native_msg_id,
                ),
                key=None,
                fallback_text=None,
            )
            resolved_relation = await resolver.resolve_relation(unresolved_relation)
            assert resolved_relation.target_event_id == event.event_id

            # ------------------------------------------------------------------
            # 9. Verify all steps through storage queries
            # ------------------------------------------------------------------
            # Both events are queryable.
            filt = EventFilter(event_kinds=["message.created", "message.text"])
            all_events = [e async for e in storage.query(filt)]
            all_ids = {e.event_id for e in all_events}
            assert event.event_id in all_ids
            assert reply_event.event_id in all_ids

            # Native ref for original event resolves correctly using adapter-provided ID.
            assert (
                await storage.resolve_native_ref(
                    "fake_presentation", "ch-0", native_msg_id
                )
                == event.event_id
            )

            # Relations for the reply are stored.
            stored_relations = await storage.list_relations(reply_event.event_id)
            assert len(stored_relations) == 1
            assert stored_relations[0].relation_type == "reply"
            assert stored_relations[0].target_event_id == event.event_id

            # Cleanup.
            await storage.close()
        finally:
            os.unlink(db_path)

    async def test_delivery_receipt_tracking(self) -> None:
        """Delivery receipts can be appended and the latest status queried."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            event = CanonicalEvent(
                event_id="receipt-evt",
                event_kind="message.text",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "track me"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            # Append receipts in sequence.
            for status in ("queued", "sent", "confirmed"):
                receipt = DeliveryReceipt(
                    receipt_id=f"rcpt-{status}",
                    event_id="receipt-evt",
                    delivery_plan_id="plan-receipt",
                    target_adapter="fake_presentation",
                    status=status,  # type: ignore[arg-type]
                )
                await storage.append_receipt(receipt)

            latest = await storage.delivery_status("plan-receipt", "fake_presentation")
            assert latest is not None
            assert latest.status == "confirmed"

            await storage.close()
        finally:
            os.unlink(db_path)

    async def test_pipeline_with_reaction_fallback(self) -> None:
        """A reaction event is downgraded when the target lacks reaction support."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            storage = SQLiteStorage(db_path=db_path)
            await storage.initialize()

            route = Route(
                id="reaction-route",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.reacted",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="fake_presentation")],
            )
            router = Router(routes=[route])
            resolver = FallbackResolver()

            event = CanonicalEvent(
                event_id="reaction-evt",
                event_kind="message.reacted",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"emoji": "👍"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            matched = router.match(event)
            assert len(matched) == 1

            target = matched[0].targets[0]
            # fake_transport has reactions="fallback", simulate target without support
            plan = resolver.resolve_fallback(event, target, {"supports_reactions": False})
            assert plan.primary_strategy.method == "direct"

            await storage.close()
        finally:
            os.unlink(db_path)
