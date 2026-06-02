"""Retry shutdown and capacity tests.

Tests shutdown safety: clean stop while sleeping, in-flight retry
capacity release, and capacity rejection.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.core.planning.delivery_plan import RetryPolicy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from tests._retry_test_helpers import (
    RetryWorker,
    _make_event,
    _make_failed_receipt,
    _make_limits,
)
from tests.helpers.async_utils import wait_until

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryShutdown:
    """Shutdown and capacity safety during retry."""

    async def test_shutdown_while_worker_sleeping(self):
        """Worker starts with long interval, stop() completes cleanly."""
        event = _make_event()
        receipt = _make_failed_receipt()
        storage = MagicMock()
        storage.list_due_retry_receipts = AsyncMock(return_value=[receipt])
        storage.get = AsyncMock(return_value=event)
        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock(
            return_value=DeliveryReceipt(
                receipt_id="rcpt-ok",
                event_id="evt-001",
                status="sent",
                created_at=datetime.now(timezone.utc),
            ),
        )
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(
            storage,
            pipeline,
            policy,
            interval=300,  # 5-minute interval — won't cycle during test
        )
        await worker.start()
        # Wait for at least one loop iteration to complete.
        await wait_until(
            lambda: storage.list_due_retry_receipts.call_count >= 1,
            timeout=2.0,
        )
        await worker.stop()

        assert worker.shutdown_event.is_set()

    async def test_shutdown_while_retry_in_flight(self):
        """Capacity slot released when retry is cancelled mid-flight.

        Uses the real RetryWorker (not the lightweight mock) so that
        stop()'s bounded cancel logic is exercised.  The delivery blocks
        on a ``proceed`` event; stop() is called while the slot is held,
        then ``proceed`` is set so the delivery completes within the
        cooperative grace period.  The capacity slot must be released
        and the call must finish.
        """
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker as RealRetryWorker

        event = _make_event()

        # Pipeline that blocks until an event fires
        proceed = asyncio.Event()
        call_completed = asyncio.Event()

        async def _slow_deliver(*args, **kwargs):
            await proceed.wait()
            call_completed.set()
            return DeliveryReceipt(
                receipt_id="rcpt-ok",
                event_id="evt-001",
                status="sent",
                created_at=datetime.now(timezone.utc),
            )

        storage = MagicMock()
        # The real worker calls claim_due_outbox_items.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obx-001",
            event_id="evt-001",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="target_a",
            attempt_number=1,
            status="retry_wait",
            next_attempt_at=(
                datetime.now(timezone.utc) - timedelta(seconds=1)
            ).isoformat(),
            receipt_id="rcpt-fail-001",
        )
        storage.claim_due_outbox_items = AsyncMock(return_value=[outbox_item])
        storage.get = AsyncMock(return_value=event)
        storage.list_receipts_for_plan = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})
        storage.mark_outbox_sent = AsyncMock(return_value=None)
        storage.mark_outbox_queued = AsyncMock(return_value=None)

        pipeline = MagicMock()
        pipeline.deliver_to_target = _slow_deliver

        limits = _make_limits(max_inflight_deliveries=1)
        capacity = CapacityController(limits)
        event_buffer = EventBuffer(maxlen=64)

        worker = RealRetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=capacity,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.5,
        )

        try:
            await worker.start()
            # Wait for the worker to claim the outbox item and acquire
            # the capacity slot (delivery is blocked on proceed).
            await wait_until(lambda: capacity.delivery_current >= 1, timeout=2.0)
            assert capacity.delivery_current >= 1

            # Call stop() while the delivery is still blocked in
            # proceed.wait().  stop() sets the shutdown event and polls;
            # we unblock the delivery so it completes within the
            # cooperative grace period.
            stop_task = asyncio.create_task(worker.stop())
            # Yield so stop() acquires the lock and sets the shutdown
            # event before we unblock the delivery.
            await asyncio.sleep(0)
            proceed.set()
            await stop_task

            assert capacity.delivery_current == 0
            assert call_completed.is_set()
        finally:
            proceed.set()
            if worker._task is not None and not worker._task.done():
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

    async def test_capacity_rejection_during_retry(self):
        """When capacity is 0, retry is rejected without calling pipeline."""
        event = _make_event()
        receipt = _make_failed_receipt()
        storage = MagicMock()
        storage.list_due_retry_receipts = AsyncMock(return_value=[receipt])
        storage.get = AsyncMock(return_value=event)
        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()
        accounting = RuntimeAccounting()

        limits = _make_limits(max_inflight_deliveries=0)
        capacity = CapacityController(limits)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(
            storage,
            pipeline,
            policy,
            capacity_controller=capacity,
            accounting=accounting,
        )
        processed = await worker._process_due(datetime.now(timezone.utc))

        assert processed == 0
        pipeline.deliver_to_target.assert_not_called()
        assert worker.state.failed == 1
        snap = accounting.snapshot()
        assert snap["capacity_rejections"] == 1


class TestRetryShutdownRealPipeline:
    """Shutdown and capacity hardening with real PipelineRunner."""

    async def test_shutdown_with_real_pipeline_and_due_receipt(self, temp_storage):
        """Real pipeline creates due receipt, worker starts then stops cleanly."""
        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats

        # Adapter that always fails transiently
        class _AlwaysTransientAdapter(FakePresentationAdapter):
            async def deliver(self, result):
                raise ConnectionError("always transient")

        adapter = _AlwaysTransientAdapter(adapter_id="shutdown_target")
        event_id = f"evt-{uuid.uuid4()}"
        event = CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_source",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "shutdown test"},
            metadata=EventMetadata(),
        )
        route = Route(
            id="shutdown-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="shutdown_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"shutdown_target": adapter}

        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters=adapters,
            event_bus=EventBus(),
            rendering_pipeline=render_pipe,
            diagnostician=Diagnostician(),
            route_stats=RouteStats(),
            runtime_accounting=accounting,
        )
        runner = PipelineRunner(config)

        ctx = AdapterContext(
            adapter_id="shutdown_target",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.shutdown_target"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        await runner.start()

        try:
            # Inject event → transient failure
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1

            limits = _make_limits(max_inflight_deliveries=1)
            capacity = CapacityController(limits)
            policy = RetryPolicy(max_attempts=3)

            worker = RetryWorker(
                temp_storage,
                runner,
                policy,
                capacity_controller=capacity,
                interval=300,
            )

            await worker.start()
            # Wait for the due-receipt processing side effect.
            await wait_until(
                lambda: worker.state.processed >= 1 or worker.state.failed >= 1,
                timeout=2.0,
            )
            await worker.stop()

            assert worker.shutdown_event.is_set()
            assert capacity.delivery_current == 0
            # No false success (the adapter always fails)
            assert worker.state.succeeded == 0
        finally:
            await runner.stop()

    async def test_capacity_rejection_during_real_retry(self, temp_storage):
        """With capacity=0, retry worker fails without calling the pipeline."""
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats

        event_id = f"evt-{uuid.uuid4()}"
        event = CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_source",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "capacity test"},
            metadata=EventMetadata(),
        )

        # Create a failed receipt directly in storage
        failed_receipt = DeliveryReceipt(
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id="plan-cap",
            target_adapter="target_a",
            route_id="route-cap",
            status="failed",
            error="ConnectionError: timeout",
            failure_kind="adapter_transient",
            next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            attempt_number=1,
            created_at=datetime.now(timezone.utc),
        )
        await temp_storage.append_receipt(failed_receipt)

        # Also store the event so the worker can look it up
        await temp_storage.append(event)

        # Build a real pipeline (but capacity=0 means it never gets called)
        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
            rendering_pipeline=render_pipe,
            diagnostician=Diagnostician(),
            route_stats=RouteStats(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        try:
            limits = _make_limits(max_inflight_deliveries=0)
            capacity = CapacityController(limits)
            accounting = RuntimeAccounting()
            policy = RetryPolicy(max_attempts=3)

            worker = RetryWorker(
                temp_storage,
                runner,
                policy,
                capacity_controller=capacity,
                accounting=accounting,
            )
            processed = await worker._process_due(datetime.now(timezone.utc))

            assert processed == 0
            assert worker.state.failed == 1
            snap = accounting.snapshot()
            assert snap["capacity_rejections"] == 1
        finally:
            await runner.stop()
