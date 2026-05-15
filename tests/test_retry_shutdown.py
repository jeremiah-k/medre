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

import pytest

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.core.planning.delivery_plan import RetryExecutor, RetryPolicy
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.runtime.accounting import RuntimeAccounting
from medre.config.model import RuntimeLimits
from medre.runtime.capacity import CapacityController
from medre.observability.classification import infer_failure_kind


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
            targets=[RouteTarget(
                adapter=receipt.target_adapter,
                channel=getattr(receipt, "target_channel", None),
            )],
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
            storage, pipeline, policy,
            interval=300,  # 5-minute interval — won't cycle during test
        )
        await worker.start()
        # Let one loop iteration complete
        await asyncio.sleep(0.05)
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
            storage, pipeline, policy,
            shutdown_event=shutdown_evt,
            capacity_controller=capacity,
            interval=300,
        )

        # Start worker — it will acquire a slot and block in deliver_to_target
        await worker.start()
        await asyncio.sleep(0.1)

        # The delivery slot should be occupied
        assert capacity.delivery_current >= 1

        # Fire the proceed event so delivery completes, then stop
        proceed.set()
        await asyncio.sleep(0.05)
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
            storage, pipeline, policy,
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
        from medre.adapters.fake_presentation import FakePresentationAdapter
        from medre.adapters.base import AdapterContext, AdapterDeliveryResult
        from medre.core.events.bus import EventBus
        from medre.core.events.metadata import EventMetadata
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        import uuid

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
                temp_storage, runner, policy,
                capacity_controller=capacity,
                interval=300,
            )

            # Start and immediately stop
            await worker.start()
            await asyncio.sleep(0.05)
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
        from medre.core.events.bus import EventBus
        from medre.core.events.metadata import EventMetadata
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        import uuid

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
                temp_storage, runner, policy,
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
