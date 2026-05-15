"""Retry lineage tests — proving no duplicate retries and dead-letter isolation.

Tests the NOT EXISTS SQL in list_due_retry_receipts, parent exclusion after
retry, child-becomes-due chaining, and dead-letter detection isolation
across independent retry chains for the same event.
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
)
from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
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


# FallbackResolver that injects a retry_policy into every plan


class _FallbackResolverWithRetry(FallbackResolver):
    """FallbackResolver that attaches a retry_policy to every DeliveryPlan."""

    def __init__(self, retry_policy: RetryPolicy) -> None:
        self._retry_policy = retry_policy

    def resolve_fallback(self, event, target, capabilities):  # type: ignore[override]
        plan = super().resolve_fallback(event, target, capabilities)
        from dataclasses import replace
        return replace(plan, retry_policy=self._retry_policy)


# Custom adapter: transient failure then succeed


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


# RetryWorker (integration variant — uses real pipeline.deliver_to_target)


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
        targets=[RouteTarget(
            adapter=receipt.target_adapter,
            channel=getattr(receipt, "target_channel", None),
        )],
    )


def _plan_from_receipt(
    receipt: DeliveryReceipt, retry_policy: RetryPolicy,
) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=receipt.delivery_plan_id,
        event_id=receipt.event_id,
        target=RouteTarget(
            adapter=receipt.target_adapter,
            channel=getattr(receipt, "target_channel", None),
        ),
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
                    source="retry",
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
        if receipt.failure_kind is not None:
            return receipt.failure_kind == "adapter_transient"
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
        payload={"body": "hello from lineage test"},
        metadata=EventMetadata(),
    )


def _build_runner(
    storage: SQLiteStorage,
    adapters: dict,
    router: Router,
    accounting: RuntimeAccounting,
    *,
    fallback_resolver: FallbackResolver | None = None,
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
        fallback_resolver=fallback_resolver or FallbackResolver(),
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


class TestRetryLineage:
    """Prove no duplicate retries, child-becomes-due, and dead-letter isolation."""

    async def test_retry_does_not_duplicate_after_success(self, temp_storage):
        """After retry succeeds, calling _process_due again produces no second
        retry.  The NOT EXISTS SQL in list_due_retry_receipts excludes parent
        receipts that already have a retry child."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="dedup_target", fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="dedup-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dedup_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"dedup_target": adapter}

        default_retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage, adapters, router, accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original_receipt = failed[0]

            future_now = original_receipt.next_retry_at + timedelta(seconds=1)

            # First retry succeeds
            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            processed = await worker._process_due(future_now)
            assert processed == 1
            assert worker.state.succeeded == 1

            # Exactly 2 receipts: 1 failed + 1 sent
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            assert len(all_receipts) == 2
            statuses = [r.status for r in all_receipts]
            assert statuses.count("failed") == 1
            assert statuses.count("sent") == 1

            # Second _process_due: NOT EXISTS excludes the parent
            due_again = await temp_storage.list_due_retry_receipts(
                future_now, limit=20, max_attempts=3,
            )
            assert len(due_again) == 0, (
                "Parent receipt should be excluded by NOT EXISTS "
                "after a retry child exists"
            )
            processed_again = await worker._process_due(future_now)
            assert processed_again == 0

            # Still exactly 2 receipts
            all_receipts_after = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            assert len(all_receipts_after) == 2
        finally:
            await runner.stop()

    async def test_failed_retry_becomes_due_child(self, temp_storage):
        """When a retry fails transiently, the child receipt becomes the next
        due receipt.  The parent is excluded by NOT EXISTS.  A subsequent
        retry of the child succeeds."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="chain_target", fail_count=2,
        )
        event = _make_event()
        route = Route(
            id="chain-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="chain_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"chain_target": adapter}

        default_retry_policy = RetryPolicy(max_attempts=5)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage, adapters, router, accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original_receipt = failed[0]
            assert original_receipt.attempt_number == 1

            # First retry: adapter still fails (attempt 2 of fail_count=2)
            first_retry_now = original_receipt.next_retry_at + timedelta(seconds=1)
            policy = RetryPolicy(max_attempts=5)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            processed = await worker._process_due(first_retry_now)
            assert processed == 1
            assert worker.state.failed == 1

            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            assert len(all_receipts) == 2

            # The child (attempt 2) should have next_retry_at
            child_receipt = [
                r for r in all_receipts
                if r.parent_receipt_id == original_receipt.receipt_id
            ]
            assert len(child_receipt) == 1
            child = child_receipt[0]
            assert child.attempt_number == 2
            assert child.next_retry_at is not None

            # Parent excluded by NOT EXISTS; only child is due
            second_retry_now = child.next_retry_at + timedelta(seconds=1)
            due = await temp_storage.list_due_retry_receipts(
                second_retry_now, limit=20, max_attempts=5,
            )
            assert len(due) >= 1
            due_ids = [r.receipt_id for r in due]
            assert child.receipt_id in due_ids
            assert original_receipt.receipt_id not in due_ids, (
                "Parent should be excluded by NOT EXISTS in favor of child"
            )

            # Second retry succeeds
            processed2 = await worker._process_due(second_retry_now)
            assert processed2 == 1
            assert worker.state.succeeded == 1

            final_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            assert len(final_receipts) == 3
            status_list = [r.status for r in final_receipts]
            assert status_list.count("failed") == 2
            assert status_list.count("sent") == 1

            sent = [r for r in final_receipts if r.status == "sent"]
            assert len(sent) == 1
            assert sent[0].attempt_number == 3
            assert sent[0].parent_receipt_id == child.receipt_id
        finally:
            await runner.stop()

    async def test_dead_letter_detection_is_lineage_specific(self, temp_storage):
        """Dead-letter detection for one target does not affect retries for a
        different target on the same event.  Two retry chains for the same
        event but different target adapters are fully isolated."""
        class _AlwaysFailAdapter(FakePresentationAdapter):
            def __init__(self):
                super().__init__(adapter_id="always_fail_target")
                self._call_count = 0

            async def deliver(self, result):
                self._call_count += 1
                raise ConnectionError(f"permanent transient #{self._call_count}")

        chain_b_adapter = _TransientThenSucceedAdapter(
            adapter_id="chain_b_target", fail_count=1,
        )
        always_fail = _AlwaysFailAdapter()

        event = _make_event()
        route = Route(
            id="lineage-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="always_fail_target"),
                RouteTarget(adapter="chain_b_target"),
            ],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {
            "always_fail_target": always_fail,
            "chain_b_target": chain_b_adapter,
        }

        default_retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage, adapters, router, accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            # Initial delivery: both fail
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 2
            statuses = {o.target_adapter: o.status for o in outcomes}
            assert statuses["always_fail_target"] == "transient_failure"
            assert statuses["chain_b_target"] == "transient_failure"

            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            failed_receipts = [r for r in all_receipts if r.status == "failed"]
            assert len(failed_receipts) == 2

            # --- Chain A: exhaust retries manually → dead-lettered ---
            fail_a = [
                r for r in failed_receipts
                if r.target_adapter == "always_fail_target"
            ][0]

            route_a = Route(
                id="lineage-route",
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="always_fail_target")],
            )
            plan_a = DeliveryPlan(
                plan_id=fail_a.delivery_plan_id,
                event_id=event.event_id,
                target=RouteTarget(adapter="always_fail_target"),
                primary_strategy=DeliveryStrategy(method="direct"),
                retry_policy=RetryPolicy(max_attempts=3),
            )

            # Attempt 2 (fails)
            try:
                await runner.deliver_to_target(
                    event, route_a, plan_a,
                    previous_receipt=fail_a, source="retry",
                )
            except Exception:
                pass

            receipts_after_2 = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            attempt_2 = [
                r for r in receipts_after_2
                if r.target_adapter == "always_fail_target"
                and r.parent_receipt_id == fail_a.receipt_id
            ][0]

            # Attempt 3 (fails → dead-lettered)
            try:
                await runner.deliver_to_target(
                    event, route_a, plan_a,
                    previous_receipt=attempt_2, source="retry",
                )
            except Exception:
                pass

            receipts_a = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            dead_a = [
                r for r in receipts_a
                if r.target_adapter == "always_fail_target"
                and r.status == "dead_lettered"
            ]
            assert len(dead_a) == 1, "Chain A should be dead-lettered"

            # --- Chain B: retry succeeds despite Chain A dead-letter ---
            fail_b = [
                r for r in failed_receipts
                if r.target_adapter == "chain_b_target"
            ][0]
            now_b = fail_b.next_retry_at + timedelta(seconds=1)

            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage, runner, policy, accounting=accounting,
            )
            processed = await worker._process_due(now_b)
            assert processed == 1
            assert worker.state.succeeded == 1, (
                "Chain B retry should succeed even though Chain A is dead-lettered"
            )

            final_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            chain_b_sent = [
                r for r in final_receipts
                if r.target_adapter == "chain_b_target" and r.status == "sent"
            ]
            assert len(chain_b_sent) == 1
            assert chain_b_sent[0].attempt_number == 2
            assert chain_b_sent[0].source == "retry"

            chain_a_dead = [
                r for r in final_receipts
                if r.target_adapter == "always_fail_target"
                and r.status == "dead_lettered"
            ]
            assert len(chain_a_dead) == 1
        finally:
            await runner.stop()
