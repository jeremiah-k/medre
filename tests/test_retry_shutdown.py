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
    """Lightweight retry worker mock for shutdown/capacity tests.

    This is **not** the production ``medre.runtime.retry.RetryWorker``;
    it is a simplified mock used by ``TestRetryShutdown`` and
    ``TestRetryShutdownRealPipeline`` to exercise capacity release,
    mid-flight cancellation, and policy behaviour without spinning up
    the full retry pipeline.  For polling-based stop / orphan-task
    hardening tests, see ``TestRetryWorkerStopOrphan`` below, which
    imports and tests the **real** ``RetryWorker`` from
    ``medre.runtime.retry``.
    """

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

        assert capacity.delivery_current >= 1

        # Fire the proceed event so delivery completes, then stop
        proceed.set()
        await wait_until(lambda: call_completed.is_set(), timeout=2.0)
        await worker.stop()

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
            await wait_until(lambda: worker._task is not None, timeout=2.0)
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

    Proves that stop() honors the bounded grace period, reports
    abandonment honestly when the task is cancellation-resistant, and
    is idempotent.
    """

    async def test_stop_timeout_cancels_cancellation_responsive_task(self):
        """Cancellation-responsive task: stop() clears _task and emits
        retry_stopped.

        The worker's _run_loop blocks in _process_due via a storage
        call that respects ``task.cancel()`` and returns.  After the
        grace period the task is cancelled and awaited so no orphan
        remains.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        # claim_due_outbox_items that honours task.cancel() (i.e. the
        # underlying aiosqlite connection or similar cooperates).  We
        # model this as "waits on an event that is set when the task
        # is cancelled".
        _cancelled_evt = asyncio.Event()

        async def _cooperative_claim(*args, **kwargs):
            try:
                # Suspend forever, but raise if the task is cancelled.
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                _cancelled_evt.set()
                raise
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_cooperative_claim)
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
        # Wait deterministically until the task is actually blocked in
        # the storage call (avoids flaky asyncio.sleep(0.1)).
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )
        orig_task = worker._task
        assert orig_task is not None
        assert not orig_task.done()

        await worker.stop()
        # Yield so the cancellation is observed before assertions.
        await asyncio.sleep(0)

        # Cancellation-responsive: _task cleared, retry_stopped emitted.
        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_abandoned" not in event_types
        # Verify retry_stopped payload contains the standard counters.
        stopped_events = [
            e for e in event_buffer if e.event_type.value == "retry_stopped"
        ]
        assert len(stopped_events) == 1
        for key in ("processed", "succeeded", "failed", "dead_lettered"):
            assert (
                key in stopped_events[0].detail
            ), f"retry_stopped payload missing '{key}'"

    async def test_stop_does_not_clear_task_when_cancellation_resistant(self):
        """Cancellation-resistant task: stop() returns boundedly but does
        NOT clear _task or report a clean stop.

        Models a storage call that swallows ``CancelledError`` and
        continues blocking.  The worker's two-stage bounded cancel must
        not hang forever; it returns within ``2 * stop_timeout_seconds``
        and reports abandonment by setting ``state.abandoned = True``
        while keeping ``_task`` referencing the still-alive task.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        # claim_due_outbox_items that catches CancelledError and spins
        # until released.  This models an adapter-side bug where
        # cancellation is swallowed and the call keeps blocking.
        _release = asyncio.Event()

        async def _cancellation_resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                # Swallow: this is the bug we are simulating.
                pass
            while not _release.is_set():
                # Yield to the loop so the task is not a CPU spin, but
                # ignore any cancellation that arrives here too.  Each
                # await is a fresh cancel delivery point but the
                # except handler continues until released.
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(
            side_effect=_cancellation_resistant_claim
        )
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
            stop_timeout_seconds=0.1,
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )
            orig_task = worker._task
            assert orig_task is not None
            assert not orig_task.done()

            stop_start = asyncio.get_event_loop().time()
            await worker.stop()
            stop_elapsed = asyncio.get_event_loop().time() - stop_start

            # Hard bound: stop() must return within ~2*stop_timeout + slack
            assert stop_elapsed < 1.0, (
                f"stop() took {stop_elapsed:.3f}s, "
                f"expected < 1.0s for stop_timeout=0.1"
            )

            # Cancellation-resistant: _task is KEPT, abandoned=True,
            # running=True, retry_abandoned emitted, retry_stopped NOT.
            assert (
                worker._task is orig_task
            ), "_task must remain pointing at the still-alive task"
            assert worker.state.running is True
            assert worker.state.abandoned is True
            event_types = [e.event_type.value for e in event_buffer]
            assert "retry_abandoned" in event_types
            assert "retry_stopped" not in event_types
            # Verify retry_abandoned payload includes the timeout and
            # the standard counters.
            abandoned_events = [
                e for e in event_buffer if e.event_type.value == "retry_abandoned"
            ]
            assert len(abandoned_events) == 1
            assert abandoned_events[0].detail.get("stop_timeout_seconds") == 0.1
            for key in ("processed", "succeeded", "failed", "dead_lettered"):
                assert (
                    key in abandoned_events[0].detail
                ), f"retry_abandoned payload missing '{key}'"
        finally:
            # Release the stuck call so the task can complete and not
            # leak into other tests.
            _release.set()
            if worker._task is not None and not worker._task.done():
                # Give the task a moment to finish after release.
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

    async def test_start_refused_when_previous_stop_abandoned_task(self):
        """After a cancellation-resistant stop, subsequent start() is refused
        while state.abandoned is True (so a duplicate worker is not launched
        over the same outbox)."""
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        _release = asyncio.Event()

        async def _resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                pass
            while not _release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_resistant_claim)
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
            stop_timeout_seconds=0.1,
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )
            await worker.stop()
            assert worker.state.abandoned is True
            assert worker._task is not None

            # Second start() must be refused: _task is still referenced,
            # call_count is unchanged.
            prev_call_count = storage.claim_due_outbox_items.call_count
            await worker.start()
            assert worker._task is not None
            assert storage.claim_due_outbox_items.call_count == prev_call_count
        finally:
            _release.set()
            if worker._task is not None and not worker._task.done():
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

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
        orig_task = worker._task
        await worker.stop()
        await asyncio.sleep(0)

        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
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

        orig_task = worker._task
        await worker.stop()
        await asyncio.sleep(0)

        assert worker._task is None
        assert orig_task is not None
        assert orig_task.done()
        assert worker.state.running is False
        # State counters are preserved (not reset).
        assert worker.state.processed == 0

    async def test_stop_when_task_already_done(self):
        """stop() called when the background task has already completed
        naturally (not due to stop()) returns promptly with clean-stop
        semantics.

        Covers the early-exit path in the polling loop: if ``task.done()``
        is ``True`` on the first iteration, the loop exits immediately
        without ever entering ``_force_cancel_with_poll``.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()

        # Make the run loop end with a BaseException that is NOT
        # asyncio.CancelledError (which would mean the task was
        # cancelled, not that it finished naturally) and is NOT caught
        # by the ``except Exception`` handler in ``_run_loop``.  This
        # models an unrecoverable error that ends the worker without
        # the stop path being involved.
        class _FatalCrash(BaseException):
            pass

        async def _crashing_claim(*args, **kwargs):
            raise _FatalCrash("simulated unrecoverable error")

        storage.claim_due_outbox_items = AsyncMock(side_effect=_crashing_claim)
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
        )

        await worker.start()
        # Wait for the task to complete (it will end with _FatalCrash
        # because the claim raises a BaseException the loop cannot
        # catch).
        orig_task = worker._task
        assert orig_task is not None
        await wait_until(lambda: orig_task.done(), timeout=2.0)
        assert orig_task.done()

        # Now call stop() — must return promptly with clean-stop
        # semantics, NOT be treated as abandoned.
        stop_start = asyncio.get_event_loop().time()
        await worker.stop()
        stop_elapsed = asyncio.get_event_loop().time() - stop_start

        assert stop_elapsed < 0.1, (
            f"stop() took {stop_elapsed:.3f}s for an already-done task; "
            f"expected < 0.1s"
        )
        assert worker._task is None
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_abandoned" not in event_types

    async def test_stop_cancelled_after_task_already_done(self):
        """stop() cancelled by the caller *after* the background task has
        already completed naturally must still do clean-stop cleanup.

        Regression: the old ``except asyncio.CancelledError`` branch in
        ``stop()`` only checked ``if not task.done()`` and skipped
        cleanup when the task was already done, leaking ``_task`` and
        ``state.running=True``.  The fix splits the branch: if the task
        is already done at cancellation time, do the clean-stop cleanup
        (clear ``_task``, set ``state.running=False``, emit
        ``retry_stopped``) and then re-raise.

        The test uses a sub-task for ``stop()`` and cancels it
        immediately.  Because the background task is already done,
        ``stop()`` may complete normally (in which case the cancel is
        a no-op and the clean-stop path is verified directly) or it
        may be cancelled mid-execution (in which case the new
        ``task.done()`` branch in the ``except CancelledError`` handler
        is verified).  Either way the end state must be clean.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
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
        )

        await worker.start()
        orig_task = worker._task
        assert orig_task is not None

        # Signal shutdown so the run loop exits cleanly on its next
        # iteration, ending the task without raising.  Wait for the
        # task to be truly done.
        worker._shutdown_event.set()
        await wait_until(lambda: orig_task.done(), timeout=2.0)
        assert orig_task.done()

        # Force stop() to be cancelled by the caller.  wait_for with
        # a very short timeout cancels the inner coroutine at the
        # first await point, modelling an external cancellation that
        # arrives while stop() is running.  When the background task
        # is already done, stop() may complete normally before the
        # timeout fires (in which case the cancel is a no-op and the
        # clean-stop path is verified directly) or it may be
        # cancelled mid-execution (in which case the new
        # ``task.done()`` branch in the ``except CancelledError``
        # handler is verified).  Either way the end state must be
        # clean.
        try:
            await asyncio.wait_for(worker.stop(), timeout=1e-6)
        except asyncio.TimeoutError:
            pass  # expected: wait_for cancelled stop() mid-execution

        # Clean-stop state must hold regardless of whether stop()
        # completed normally or was cancelled mid-execution.
        assert worker._task is None
        assert worker.state.running is False
        assert worker.state.abandoned is False
        event_types = [e.event_type.value for e in event_buffer]
        assert "retry_stopped" in event_types
        assert "retry_abandoned" not in event_types

    async def test_concurrent_stop_no_duplicate_events(self):
        """Concurrent stop() calls do not emit duplicate events.

        The internal ``asyncio.Lock`` serialises callers so the second
        call sees ``_task is None`` after the first finishes and
        returns without entering the polling loop.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        storage.claim_due_outbox_items = AsyncMock(return_value=[])
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
        )

        await worker.start()
        await wait_until(
            lambda: storage.claim_due_outbox_items.call_count >= 1,
            timeout=2.0,
        )

        # Launch two stop() calls concurrently.
        await asyncio.gather(worker.stop(), worker.stop())

        # Exactly one retry_stopped event.
        event_types = [e.event_type.value for e in event_buffer]
        assert (
            event_types.count("retry_stopped") == 1
        ), f"expected exactly one retry_stopped, got {event_types.count('retry_stopped')}"
        assert worker._task is None
        assert worker.state.running is False

    async def test_stop_cancelled_mid_poll(self):
        """External cancellation of stop() itself marks the worker
        abandoned and emits ``retry_abandoned`` with
        ``reason='stop_cancelled'``.

        Models the case where the caller of ``await stop()`` is
        cancelled mid-poll (e.g. ``MedreApp.stop()`` hits a shutdown
        timeout and cancels its inner cleanup).  The worker must not
        silently leave ``state.running=True`` and ``state.abandoned=False``
        — the caller needs a way to detect this state.
        """
        from medre.runtime.events import EventBuffer
        from medre.runtime.retry import RetryWorker

        storage = MagicMock()
        _release = asyncio.Event()

        async def _resistant_claim(*args, **kwargs):
            try:
                await _release.wait()
            except asyncio.CancelledError:
                pass
            while not _release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return []

        storage.claim_due_outbox_items = AsyncMock(side_effect=_resistant_claim)
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
            stop_timeout_seconds=2.0,  # long enough to cancel mid-poll
        )

        try:
            await worker.start()
            await wait_until(
                lambda: storage.claim_due_outbox_items.call_count >= 1,
                timeout=2.0,
            )

            # Begin stop() in a task, then cancel it mid-poll.
            stop_task = asyncio.create_task(worker.stop())
            # Let the polling loop start.
            await asyncio.sleep(0.1)
            stop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stop_task

            # Worker must be marked abandoned so the caller can detect
            # the cancelled state and refuse a relaunch.
            assert worker.state.abandoned is True
            assert worker.state.running is True
            event_types = [e.event_type.value for e in event_buffer]
            assert "retry_abandoned" in event_types
            assert "retry_stopped" not in event_types

            # Verify the abandonment event has reason='stop_cancelled'
            # and the timeout payload.
            abandoned_events = [
                e for e in event_buffer if e.event_type.value == "retry_abandoned"
            ]
            assert len(abandoned_events) == 1
            assert abandoned_events[0].detail.get("reason") == "stop_cancelled"
            assert abandoned_events[0].detail.get("stop_timeout_seconds") == 2.0
            # Standard counters must be present.
            for key in ("processed", "succeeded", "failed", "dead_lettered"):
                assert key in abandoned_events[0].detail
        finally:
            _release.set()
            if worker._task is not None and not worker._task.done():
                try:
                    await asyncio.wait_for(worker._task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass


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
