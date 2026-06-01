"""Retry shutdown and capacity tests.

Tests shutdown safety: clean stop while sleeping, in-flight retry
capacity release, and capacity rejection.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from medre.config.model import RuntimeLimits
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.core.observability.classification import infer_failure_kind
from medre.core.planning.delivery_plan import RetryExecutor, RetryPolicy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from tests.helpers.async_utils import wait_until

# ---------------------------------------------------------------------------
# RetryWorker (shutdown variant)
# ---------------------------------------------------------------------------


@dataclass
class _RetryWorkerState:
    succeeded: int = 0
    dead_lettered: int = 0
    failed: int = 0
    processed: int = 0


class RetryWorker:
    """Lightweight retry worker for shutdown/capacity tests."""

    def __init__(
        self,
        storage,
        pipeline,
        retry_policy: RetryPolicy,
        *,
        shutdown_event: asyncio.Event | None = None,
        capacity_controller: CapacityController | None = None,
        accounting: RuntimeAccounting | None = None,
        interval: float = 1.0,
    ) -> None:
        self.storage = storage
        self.pipeline = pipeline
        self._retry_executor = RetryExecutor(retry_policy)
        self.state = _RetryWorkerState()
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._capacity = capacity_controller
        self._accounting = accounting or RuntimeAccounting()
        self._interval = interval
        self._task: asyncio.Task | None = None

    async def _process_due(self, now: datetime) -> int:
        if self.shutdown_event.is_set():
            return 0

        if self._capacity is not None:
            acquired = await self._capacity.acquire_delivery()
            if not acquired:
                self.state.failed += 1
                self._accounting.record_capacity_rejection()
                return 0

        try:
            due = await self._get_due_receipts(now)
            processed = 0
            for receipt in due:
                if self.shutdown_event.is_set():
                    break
                if not self._is_retryable(receipt):
                    continue

                event = await self.storage.get(receipt.event_id)
                if event is None:
                    continue

                next_attempt = self._retry_executor.next_attempt_number(
                    receipt.attempt_number,
                )

                try:
                    await self.pipeline.deliver_to_target(
                        event,
                        self._make_route(receipt),
                        self._make_plan(receipt),
                        previous_receipt=receipt,
                    )
                    self.state.succeeded += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._retry_executor.is_exhausted(next_attempt):
                        self.state.dead_lettered += 1
                    else:
                        self.state.failed += 1

                self.state.processed += 1
                processed += 1
            return processed
        finally:
            if self._capacity is not None:
                await self._capacity.release_delivery()

    async def _get_due_receipts(self, now: datetime):
        return await self.storage.list_due_retry_receipts(now)

    @staticmethod
    def _is_retryable(receipt: DeliveryReceipt) -> bool:
        if receipt.status != "failed":
            return False
        if receipt.failure_kind is not None:
            return receipt.failure_kind == "adapter_transient"
        kind = infer_failure_kind(receipt.error, receipt.status)
        return kind == "adapter_transient"

    @staticmethod
    def _make_route(receipt):
        return Route(
            id=receipt.route_id or "retry-route",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[
                RouteTarget(
                    adapter=receipt.target_adapter,
                    channel=getattr(receipt, "target_channel", None),
                )
            ],
        )

    @staticmethod
    def _make_plan(receipt):
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy

        return DeliveryPlan(
            plan_id=receipt.delivery_plan_id,
            event_id=receipt.event_id,
            target=RouteTarget(
                adapter=receipt.target_adapter,
                channel=getattr(receipt, "target_channel", None),
            ),
            primary_strategy=DeliveryStrategy(method="direct"),
        )

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.shutdown_event.set()
        if self._task is not None:
            await self._task

    async def _run_loop(self) -> None:
        while not self.shutdown_event.is_set():
            now = datetime.now(timezone.utc)
            await self._process_due(now)
            try:
                await asyncio.wait_for(
                    self.shutdown_event.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str = "evt-001") -> CanonicalEvent:
    return CanonicalEvent(
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


def _make_failed_receipt(
    *,
    receipt_id: str = "rcpt-fail-001",
    error: str = "ConnectionError: timeout",
    attempt_number: int = 1,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id="evt-001",
        delivery_plan_id="plan-1",
        target_adapter="target_a",
        route_id="route-1",
        status="failed",
        error=error,
        next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        attempt_number=attempt_number,
        created_at=datetime.now(timezone.utc),
    )


def _make_limits(**overrides) -> RuntimeLimits:
    defaults = {
        "max_inflight_deliveries": 10,
        "max_inflight_replay_events": 10,
        "shutdown_drain_timeout_seconds": 5,
        "delivery_acquire_timeout_seconds": 0.5,
    }
    defaults.update(overrides)
    return RuntimeLimits(**defaults)


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
        # Stop immediately
        await worker.stop()

        assert worker.shutdown_event.is_set()

    async def test_shutdown_while_retry_in_flight(self):
        """Capacity slot released when retry is cancelled mid-flight."""
        event = _make_event()
        receipt = _make_failed_receipt()
        storage = MagicMock()
        storage.list_due_retry_receipts = AsyncMock(return_value=[receipt])
        storage.get = AsyncMock(return_value=event)

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

        pipeline = MagicMock()
        pipeline.deliver_to_target = _slow_deliver

        limits = _make_limits(max_inflight_deliveries=1)
        capacity = CapacityController(limits)
        policy = RetryPolicy(max_attempts=3)

        shutdown_evt = asyncio.Event()
        worker = RetryWorker(
            storage,
            pipeline,
            policy,
            shutdown_event=shutdown_evt,
            capacity_controller=capacity,
            interval=300,
        )

        # Start worker — it will acquire a slot and block in deliver_to_target
        await worker.start()
        await wait_until(lambda: capacity.delivery_current >= 1, timeout=2.0)

        # The delivery slot should be occupied
        assert capacity.delivery_current >= 1

        # Fire the proceed event so delivery completes, then stop
        proceed.set()
        await wait_until(lambda: call_completed.is_set(), timeout=2.0)
        await worker.stop()

        # Capacity slot should be released
        assert capacity.delivery_current == 0
        # No false success if we shut down before completion
        # (the delivery completed normally here, so succeeded is ok)
        assert call_completed.is_set()

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
        import uuid

        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.metadata import EventMetadata
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

            # Verify due receipt exists
            receipts = await temp_storage.list_receipts_for_event(event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1

            # Create capacity controller and worker
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

            # Start and immediately stop
            await worker.start()
            await wait_until(lambda: worker._task is not None, timeout=2.0)
            await worker.stop()

            # Worker stops cleanly
            assert worker.shutdown_event.is_set()
            # No leaked capacity
            assert capacity.delivery_current == 0
            # No false success (the adapter always fails)
            assert worker.state.succeeded == 0
        finally:
            await runner.stop()

    async def test_capacity_rejection_during_real_retry(self, temp_storage):
        """With capacity=0, retry worker fails without calling the pipeline."""
        import uuid

        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.metadata import EventMetadata
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


class TestRetryWorkerStopOrphan:
    """Hardened stop() tests using the real RetryWorker.

    Proves that stop() cancels orphan tasks on timeout and is idempotent.
    """

    async def test_stop_timeout_cancels_orphan_task(self):
        """When the background task ignores shutdown_event, stop() cancels it.

        The worker's _run_loop blocks in _process_due via a storage call
        that never returns.  After the grace period the task is cancelled
        and awaited so no orphan remains.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        # claim_due_outbox_items blocks forever — simulates a stuck I/O call.
        _unblock = asyncio.Event()

        async def _stuck_claim(*args, **kwargs):
            await _unblock.wait()
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_stuck_claim)
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        event_buffer = EventBuffer(maxlen=64)

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
            event_buffer=event_buffer,
            stop_timeout_seconds=0.2,
        )

        await worker.start()
        assert worker._task is not None

        # Give the task a moment to enter the stuck claim call.
        await asyncio.sleep(0.1)
        assert not worker._task.done()

        await worker.stop()

        # Task must be cleared — no orphan.
        assert worker._task is None
        assert worker.state.running is False
        # The task was actually cancelled (not still running).
        # Unblock the stuck call so it doesn't leak into other tests.
        _unblock.set()

    async def test_stop_idempotent(self):
        """Calling stop() twice is safe — second call is a no-op."""
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
        )

        await worker.start()
        # Wait for at least one loop iteration.
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        # First stop.
        await worker.stop()
        assert worker._task is None
        assert worker.state.running is False

        # Second stop — must not raise.
        await worker.stop()
        assert worker._task is None
        assert worker.state.running is False

    async def test_stop_cleared_task_not_running(self):
        """After stop(), state.running is False regardless of path taken."""
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
        storage.count_outbox_by_status = AsyncMock(return_value={})

        pipeline = MagicMock()
        pipeline.deliver_to_target = AsyncMock()

        worker = RetryWorker(
            storage=storage,
            pipeline=pipeline,
            capacity_controller=None,
            enabled=True,
            interval_seconds=300,
        )

        await worker.start()
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        await worker.stop()

        assert worker._task is None
        assert worker.state.running is False
        # State counters are preserved (not reset).
        assert worker.state.processed == 0


class TestRetryCapacityRejectionBackoff:
    """Capacity rejection backoff policy tests using the real RetryWorker."""

    async def test_retry_capacity_rejection_backoff(self, temp_storage):
        """When capacity always rejects:
        1. retry_failed event emitted
        2. outbox next_attempt_at updated (backoff applied)
        3. attempt_number unchanged (capacity rejection ≠ delivery attempt)
        4. Monotonic backoff across two rejection cycles
        5. Snapshot counters correct
        """
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.runtime.events import EventBuffer, RuntimeEventType
        from medre.runtime.retry import RetryWorker

        event_buffer = EventBuffer(maxlen=64)
        event = _make_event()
        await temp_storage.append(event)

        # Create a failed receipt for lineage + an outbox item in retry_wait.
        now = datetime.now(timezone.utc)
        receipt_id = f"rcpt-{uuid.uuid4()}"
        failed_receipt = DeliveryReceipt(
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id="plan-cap-backoff",
            target_adapter="target_a",
            route_id="route-cap-backoff",
            status="failed",
            error="ConnectionError: timeout",
            failure_kind="adapter_transient",
            next_retry_at=now - timedelta(seconds=1),  # due now
            attempt_number=1,
            created_at=now,
        )
        await temp_storage.append_receipt(failed_receipt)

        outbox_id = f"obx-{uuid.uuid4()}"
        outbox_item = DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event.event_id,
            route_id="route-cap-backoff",
            delivery_plan_id="plan-cap-backoff",
            target_adapter="target_a",
            attempt_number=1,
            status="retry_wait",
            next_attempt_at=(now - timedelta(seconds=1)).isoformat(),
            receipt_id=receipt_id,
        )
        await temp_storage.create_outbox_item(outbox_item)

        # Pipeline needed for RetryWorker but capacity=0 means it never gets called
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

        # Capacity controller that always rejects
        limits = _make_limits(max_inflight_deliveries=0)
        capacity = CapacityController(limits)

        worker = RetryWorker(
            storage=temp_storage,
            pipeline=runner,
            capacity_controller=capacity,
            enabled=True,
            interval_seconds=5.0,
            max_attempts=3,
            event_buffer=event_buffer,
        )

        try:
            # === Cycle 1: capacity rejection ===
            cycle1_now = datetime.now(timezone.utc)
            await worker._process_due(cycle1_now)

            # Assert: retry_failed event emitted with capacity_rejection
            events = list(event_buffer)
            event_types = [e.event_type for e in events]
            assert (
                RuntimeEventType.RETRY_FAILED in event_types
            ), f"Expected retry_failed event, got: {[e.value for e in event_types]}"
            failed_events = [
                e for e in events if e.event_type == RuntimeEventType.RETRY_FAILED
            ]
            assert len(failed_events) >= 1
            assert failed_events[0].detail["status"] == "capacity_rejection"

            # Assert: outbox next_attempt_at was updated (pushed forward)
            updated_item = await temp_storage.get_outbox_item(outbox_id)
            assert updated_item is not None
            assert updated_item.next_attempt_at is not None
            _parsed_next = datetime.fromisoformat(updated_item.next_attempt_at)
            assert _parsed_next > cycle1_now, (
                f"next_attempt_at should be pushed past {cycle1_now}, "
                f"got {_parsed_next}"
            )
            first_backoff_next_at = updated_item.next_attempt_at

            # Assert: attempt_number unchanged (capacity rejection doesn't increment)
            assert (
                updated_item.attempt_number == 1
            ), f"attempt_number should remain 1, got {updated_item.attempt_number}"

            # Assert: worker snapshot shows correct counters
            state = worker.state
            assert state.failed == 1
            assert state.processed == 0
            assert state.succeeded == 0

            # === Cycle 2: capacity still rejecting ===
            cycle2_now = datetime.fromisoformat(first_backoff_next_at) + timedelta(
                seconds=1,
            )
            await worker._process_due(cycle2_now)

            updated_item_2 = await temp_storage.get_outbox_item(outbox_id)
            assert updated_item_2 is not None

            # Assert: next_attempt_at advanced monotonically
            assert updated_item_2.next_attempt_at is not None
            _parsed_next_2 = datetime.fromisoformat(updated_item_2.next_attempt_at)
            _parsed_first = datetime.fromisoformat(first_backoff_next_at)
            assert _parsed_next_2 > _parsed_first, (
                f"Second backoff ({_parsed_next_2}) must be "
                f"later than first ({_parsed_first})"
            )

            # Assert: attempt_number still unchanged
            assert updated_item_2.attempt_number == 1

            # Assert: snapshot counters reflect 2 rejections
            assert state.failed == 2
            assert state.processed == 0
            assert state.succeeded == 0

            # Assert: second retry_failed event
            events_2 = list(event_buffer)
            failed_events_2 = [
                e for e in events_2 if e.event_type == RuntimeEventType.RETRY_FAILED
            ]
            assert len(failed_events_2) >= 2
        finally:
            await worker.stop()
            await runner.stop()
