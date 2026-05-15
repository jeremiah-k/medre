"""Retry integration tests through the real PipelineRunner with fake adapters.

Tests the full retry flow: initial delivery fails transiently, receipt is
recorded, RetryWorker picks it up and re-delivers through the real pipeline.
Uses FaultyPresentationAdapter and custom transient-fail adapters.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.base import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
)
from medre.adapters.fake_presentation import (
    FakePresentationAdapter,
    FaultyPresentationAdapter,
)
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.bus import EventBus
from medre.core.events.metadata import EventMetadata
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.storage.sqlite import SQLiteStorage
from medre.observability.classification import infer_failure_kind


# ---------------------------------------------------------------------------
# Custom adapter: transient failure then succeed
# ---------------------------------------------------------------------------


class _TransientThenSucceedAdapter(FakePresentationAdapter):
    """Raises ConnectionError for the first N deliveries, then succeeds."""

    def __init__(
        self,
        adapter_id: str = "transient_adapter",
        fail_count: int = 1,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._fail_count = fail_count
        self._call_count: int = 0

    async def deliver(self, result) -> AdapterDeliveryResult | None:
        self._call_count += 1
        if self._call_count <= self._fail_count:
            raise ConnectionError(
                f"Transient failure #{self._call_count} from {self.adapter_id}"
            )
        return await super().deliver(result)

    @property
    def call_count(self) -> int:
        return self._call_count


# ---------------------------------------------------------------------------
# RetryWorker (integration variant — uses real pipeline.deliver_to_target)
# ---------------------------------------------------------------------------


@dataclass
class _RetryWorkerState:
    succeeded: int = 0
    dead_lettered: int = 0
    failed: int = 0
    processed: int = 0


def _route_from_receipt(receipt: DeliveryReceipt) -> Route:
    return Route(
        id=receipt.route_id or "retry-route",
        source=RouteSource(adapter=None, event_kinds=(), channel=None),
        targets=[RouteTarget(adapter=receipt.target_adapter)],
    )


def _plan_from_receipt(
    receipt: DeliveryReceipt, retry_policy: RetryPolicy,
) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=receipt.delivery_plan_id,
        event_id=receipt.event_id,
        target=RouteTarget(adapter=receipt.target_adapter),
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=retry_policy,
    )


class _RetryWorker:
    """Lightweight retry worker that delegates to PipelineRunner.deliver_to_target."""

    def __init__(
        self,
        storage,
        pipeline: PipelineRunner,
        retry_policy: RetryPolicy,
        *,
        accounting: RuntimeAccounting | None = None,
    ) -> None:
        self.storage = storage
        self.pipeline = pipeline
        self._retry_executor = RetryExecutor(retry_policy)
        self.state = _RetryWorkerState()
        self._shutdown = asyncio.Event()
        self._accounting = accounting or RuntimeAccounting()

    async def _process_due(self, now: datetime) -> int:
        if self._shutdown.is_set():
            return 0

        due = await self.storage.list_due_retry_receipts(now)
        processed = 0

        for receipt in due:
            if self._shutdown.is_set():
                break
            if not self._is_retryable(receipt):
                continue

            event = await self.storage.get(receipt.event_id)
            if event is None:
                continue

            next_attempt = self._retry_executor.next_attempt_number(
                receipt.attempt_number,
            )
            route = _route_from_receipt(receipt)
            plan = _plan_from_receipt(receipt, self._retry_executor.policy)

            try:
                await self.pipeline.deliver_to_target(
                    event, route, plan,
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

    @staticmethod
    def _is_retryable(receipt: DeliveryReceipt) -> bool:
        if receipt.status != "failed":
            return False
        kind = infer_failure_kind(receipt.error, receipt.status)
        return kind == "adapter_transient"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str | None = None) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id or f"evt-{uuid.uuid4()}",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_source",
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello from integration test"},
        metadata=EventMetadata(),
    )


def _build_runner(
    storage: SQLiteStorage,
    adapters: dict,
    router: Router,
    accounting: RuntimeAccounting,
) -> PipelineRunner:
    render_pipe = RenderingPipeline()
    render_pipe.register(TextRenderer(), priority=100)
    for aid, adapter in adapters.items():
        platform = getattr(adapter, "platform", None)
        if isinstance(platform, str):
            render_pipe.register_platforms_from({aid: platform})

    config = PipelineConfig(
        storage=storage,
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters,
        event_bus=EventBus(),
        rendering_pipeline=render_pipe,
        diagnostician=Diagnostician(),
        route_stats=RouteStats(),
        runtime_accounting=accounting,
    )
    return PipelineRunner(config)


async def _start_adapters(adapters: dict) -> None:
    for aid, adapter in adapters.items():
        ctx = AdapterContext(
            adapter_id=aid,
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger(f"test.{aid}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryRuntimeIntegration:
    """Retry through the real PipelineRunner with fake adapters."""

    async def test_fake_bridge_transient_then_retry(self, temp_storage):
        """Transient failure on first attempt, retry succeeds."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="transient_target", fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="test-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="transient_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"transient_target": adapter}

        runner = _build_runner(temp_storage, adapters, router, accounting)
        await _start_adapters(adapters)
        await runner.start()

        try:
            # First delivery: adapter raises ConnectionError → transient failure
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Find the failed receipt in storage
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) >= 1
            original_receipt = failed[0]
            assert "ConnectionError" in (original_receipt.error or "")

            # Mock list_due_retry_receipts to return the failed receipt
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=[original_receipt],
            )

            # Retry via worker
            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            processed = await worker._process_due(datetime.now(timezone.utc))

            assert processed == 1
            assert worker.state.succeeded == 1

            # Verify retry receipt lineage
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            retry_receipts = [
                r for r in all_receipts
                if r.parent_receipt_id == original_receipt.receipt_id
            ]
            assert len(retry_receipts) >= 1
            retry_rcpt = retry_receipts[0]
            assert retry_rcpt.attempt_number == 2
            assert retry_rcpt.parent_receipt_id == original_receipt.receipt_id

            # Verify native ref persisted on retry success
            native_refs = await temp_storage.list_native_refs_for_event(
                event.event_id,
            )
            assert len(native_refs) >= 1
        finally:
            await runner.stop()

    async def test_fanout_one_transient_then_retry(self, temp_storage):
        """Two targets: one succeeds, one fails transiently; only failed retried."""
        good_adapter = FakePresentationAdapter(
            adapter_id="good_target",
        )
        bad_adapter = _TransientThenSucceedAdapter(
            adapter_id="bad_target", fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="fanout-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="good_target"),
                RouteTarget(adapter="bad_target"),
            ],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"good_target": good_adapter, "bad_target": bad_adapter}

        runner = _build_runner(temp_storage, adapters, router, accounting)
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 2
            statuses = {o.target_adapter: o.status for o in outcomes}
            assert statuses["good_target"] == "success"
            assert statuses["bad_target"] == "transient_failure"

            # Get only the failed receipt for the bad target
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed_bad = [
                r for r in receipts
                if r.status == "failed" and r.target_adapter == "bad_target"
            ]
            assert len(failed_bad) == 1

            # Mock list_due_retry_receipts to return only the bad target's receipt
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=failed_bad,
            )

            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            processed = await worker._process_due(datetime.now(timezone.utc))

            assert processed == 1
            assert worker.state.succeeded == 1

            # Verify only bad target was retried
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            bad_target_receipts = [
                r for r in all_receipts if r.target_adapter == "bad_target"
            ]
            # Should have: original failed + retry success
            succeeded = [
                r for r in bad_target_receipts if r.status == "sent"
            ]
            assert len(succeeded) >= 1

            # Good target should NOT be duplicated
            good_target_receipts = [
                r for r in all_receipts if r.target_adapter == "good_target"
            ]
            # Only the original success receipt for good target
            good_succeeded = [
                r for r in good_target_receipts if r.status == "sent"
            ]
            assert len(good_succeeded) == 1
        finally:
            await runner.stop()

    async def test_accounting_reflects_retry(self, temp_storage):
        """After retry succeeds, accounting shows correct counts."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="accounting_target", fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="acct-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="accounting_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"accounting_target": adapter}

        runner = _build_runner(temp_storage, adapters, router, accounting)
        await _start_adapters(adapters)
        await runner.start()

        try:
            # Initial delivery: fails transiently
            await runner.handle_ingress(event)

            snap_after_fail = accounting.snapshot()
            assert snap_after_fail["inbound_accepted"] == 1
            assert snap_after_fail["outbound_attempts"] == 1
            assert snap_after_fail["outbound_failed"] == 1
            assert snap_after_fail["outbound_delivered"] == 0

            # Get failed receipt and retry
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) >= 1

            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=failed,
            )

            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            await worker._process_due(datetime.now(timezone.utc))

            snap_after_retry = accounting.snapshot()
            # Original failure counted (via handle_ingress → _deliver_one)
            assert snap_after_retry["outbound_failed"] == 1
            # deliver_to_target does not update accounting (that happens
            # at the _deliver_to_targets_inner level).  The worker tracks
            # its own success state separately.
            assert worker.state.succeeded == 1
            # Initial attempt was counted by the full pipeline path
            assert snap_after_retry["outbound_attempts"] == 1
        finally:
            await runner.stop()
