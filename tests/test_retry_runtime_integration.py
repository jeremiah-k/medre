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

from medre.adapters.fakes.presentation import (
    FakePresentationAdapter,
)
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
)
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.core.observability.classification import infer_failure_kind
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
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting

# ---------------------------------------------------------------------------
# FallbackResolver that injects a retry_policy into every plan
# ---------------------------------------------------------------------------


class _FallbackResolverWithRetry(FallbackResolver):
    """FallbackResolver that attaches a retry_policy to every DeliveryPlan."""

    def __init__(self, retry_policy: RetryPolicy) -> None:
        self._retry_policy = retry_policy

    def resolve_fallback(self, event, target, capabilities, **kwargs):  # type: ignore[override]
        plan = super().resolve_fallback(event, target, capabilities, **kwargs)
        from dataclasses import replace

        return replace(plan, retry_policy=self._retry_policy)


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


class _AlwaysPermanentFailAdapter(FakePresentationAdapter):
    """Always raises ValueError (permanent failure, not retryable)."""

    def __init__(self, adapter_id: str = "permanent_fail_adapter") -> None:
        super().__init__(adapter_id=adapter_id)

    async def deliver(self, result) -> AdapterDeliveryResult | None:
        raise ValueError(f"Permanent failure from {self.adapter_id}: bad payload")


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
        targets=[
            RouteTarget(
                adapter=receipt.target_adapter,
                channel=getattr(receipt, "target_channel", None),
            )
        ],
    )


def _plan_from_receipt(
    receipt: DeliveryReceipt,
    retry_policy: RetryPolicy,
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
                    event,
                    route,
                    plan,
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
        # Use the persisted failure_kind when available; fall back to
        # error-pattern inference for receipts that lack it.
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
        payload={"body": "hello from integration test"},
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


class TestRetryRuntimeIntegration:
    """Retry through the real PipelineRunner with fake adapters."""

    async def test_fake_bridge_transient_then_retry(self, temp_storage):
        """Transient failure on first attempt, retry succeeds."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="transient_target",
            fail_count=1,
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
                temp_storage,
                runner,
                policy,
                accounting=accounting,
            )
            processed = await worker._process_due(datetime.now(timezone.utc))

            assert processed == 1
            assert worker.state.succeeded == 1

            # Verify retry receipt lineage
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            retry_receipts = [
                r
                for r in all_receipts
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
            adapter_id="bad_target",
            fail_count=1,
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
                r
                for r in receipts
                if r.status == "failed" and r.target_adapter == "bad_target"
            ]
            assert len(failed_bad) == 1

            # Mock list_due_retry_receipts to return only the bad target's receipt
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=failed_bad,
            )

            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage,
                runner,
                policy,
                accounting=accounting,
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
            succeeded = [r for r in bad_target_receipts if r.status == "sent"]
            assert len(succeeded) >= 1

            # Good target should NOT be duplicated
            good_target_receipts = [
                r for r in all_receipts if r.target_adapter == "good_target"
            ]
            # Only the original success receipt for good target
            good_succeeded = [r for r in good_target_receipts if r.status == "sent"]
            assert len(good_succeeded) == 1
        finally:
            await runner.stop()

    async def test_accounting_reflects_retry(self, temp_storage):
        """After retry succeeds, accounting shows correct counts."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="accounting_target",
            fail_count=1,
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
                temp_storage,
                runner,
                policy,
                accounting=accounting,
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

    async def test_real_transient_failure_produces_due_receipt(self, temp_storage):
        """Real pipeline transient failure creates receipt with next_retry_at,
        then retry through worker succeeds with correct lineage."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="due_target",
            fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="due-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="due_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"due_target": adapter}

        default_retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            # Inject event → adapter raises ConnectionError → transient failure
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Get the failed receipt from storage
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original_receipt = failed[0]

            # Assert receipt has all transient-failure markers
            assert original_receipt.failure_kind == "adapter_transient"
            assert original_receipt.next_retry_at is not None
            assert original_receipt.attempt_number == 1
            assert original_receipt.status == "failed"

            # Advance time past next_retry_at and verify storage query finds it
            future_now = original_receipt.next_retry_at + timedelta(seconds=1)
            due = await temp_storage.list_due_retry_receipts(future_now)
            assert len(due) >= 1
            assert due[0].receipt_id == original_receipt.receipt_id

            # Retry via worker (uses real storage query)
            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage,
                runner,
                policy,
                accounting=accounting,
            )
            processed = await worker._process_due(future_now)

            assert processed == 1
            assert worker.state.succeeded == 1

            # Verify retry receipt lineage
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            retry_receipts = [
                r
                for r in all_receipts
                if r.parent_receipt_id == original_receipt.receipt_id
            ]
            assert len(receipts) >= 1
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

    async def test_retry_fanout_correctness(self, temp_storage):
        """One event fans out to 3 targets: A succeeds, B transient then retry
        succeeds, C permanent failure (not retried).  Verifies correct receipt
        counts, retryability, and accounting totals."""
        adapter_a = FakePresentationAdapter(adapter_id="target_a")
        adapter_b = _TransientThenSucceedAdapter(
            adapter_id="target_b",
            fail_count=1,
        )
        adapter_c = _AlwaysPermanentFailAdapter(adapter_id="target_c")

        event = _make_event()
        route = Route(
            id="fanout-3-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[
                RouteTarget(adapter="target_a"),
                RouteTarget(adapter="target_b"),
                RouteTarget(adapter="target_c"),
            ],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {
            "target_a": adapter_a,
            "target_b": adapter_b,
            "target_c": adapter_c,
        }

        default_retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 3
            statuses = {o.target_adapter: o.status for o in outcomes}
            assert statuses["target_a"] == "success"
            assert statuses["target_b"] == "transient_failure"
            assert statuses["target_c"] == "permanent_failure"

            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )

            # --- Target A: 1 sent receipt, no retry ---
            a_receipts = [r for r in all_receipts if r.target_adapter == "target_a"]
            assert len(a_receipts) == 1
            assert a_receipts[0].status == "sent"
            assert a_receipts[0].next_retry_at is None

            # --- Target B: 1 failed receipt with next_retry_at ---
            b_failed = [
                r
                for r in all_receipts
                if r.target_adapter == "target_b" and r.status == "failed"
            ]
            assert len(b_failed) == 1
            b_original = b_failed[0]
            assert b_original.failure_kind == "adapter_transient"
            assert b_original.next_retry_at is not None

            # --- Target C: 1 failed receipt, NOT retryable ---
            c_receipts = [r for r in all_receipts if r.target_adapter == "target_c"]
            c_failed = [r for r in c_receipts if r.status == "failed"]
            assert len(c_failed) >= 1
            c_orig = c_failed[0]
            assert c_orig.failure_kind not in (
                None,
                "adapter_transient",
            ), "Target C should have permanent failure_kind, not transient"
            assert (
                c_orig.next_retry_at is None
            ), "Permanent failure should not have next_retry_at"

            # --- Retry only B's receipt ---
            policy = RetryPolicy(max_attempts=3)
            worker = _RetryWorker(
                temp_storage,
                runner,
                policy,
                accounting=accounting,
            )
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=[b_original],
            )
            processed = await worker._process_due(
                b_original.next_retry_at + timedelta(seconds=1),
            )
            assert processed == 1
            assert worker.state.succeeded == 1

            # --- Verify total receipts ---
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            a_total = [r for r in all_receipts if r.target_adapter == "target_a"]
            b_total = [r for r in all_receipts if r.target_adapter == "target_b"]
            [r for r in all_receipts if r.target_adapter == "target_c"]

            assert len(a_total) == 1
            assert len(b_total) == 2  # original failed + retry succeeded
            b_sent = [r for r in b_total if r.status == "sent"]
            assert len(b_sent) == 1
            assert b_sent[0].parent_receipt_id == b_original.receipt_id

            # Accounting: original fanout counted by handle_ingress
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 1
            assert snap["outbound_attempts"] == 3
            assert snap["outbound_delivered"] == 1  # target_a only
            assert snap["outbound_failed"] == 2  # target_b + target_c
            assert snap["loop_prevented"] == 0
        finally:
            await runner.stop()

    async def test_real_bridge_retry_integration(self, temp_storage):
        """Full bridge retry: real pipeline, real storage query, native ref
        on retry success, original failed excluded from next due query."""
        from medre.config.model import RuntimeLimits
        from medre.core.supervision.capacity import CapacityController
        from medre.runtime.retry import RetryWorker

        adapter = _TransientThenSucceedAdapter(
            adapter_id="bridge_target",
            fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="bridge-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="bridge_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"bridge_target": adapter}

        default_retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(default_retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            # Inject event → adapter raises ConnectionError → transient failure
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Get the failed receipt from storage
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original_receipt = failed[0]
            assert original_receipt.failure_kind == "adapter_transient"
            assert original_receipt.next_retry_at is not None

            # Advance time past next_retry_at
            future_now = original_receipt.next_retry_at + timedelta(seconds=1)

            # Use the real RetryWorker with same storage and pipeline
            limits = RuntimeLimits(
                max_inflight_deliveries=10,
                max_inflight_replay_events=10,
                shutdown_drain_timeout_seconds=5,
                delivery_acquire_timeout_seconds=0.5,
            )
            capacity = CapacityController(limits)
            worker = RetryWorker(
                temp_storage,
                runner,
                capacity,
                enabled=True,
                interval_seconds=10.0,
                batch_size=20,
                max_attempts=3,
            )

            # Process due receipts
            await worker._process_due(future_now)

            # Assert: retry succeeded
            assert worker.state.succeeded == 1
            assert worker.state.processed == 1

            # Assert: native ref created on retry success
            native_refs = await temp_storage.list_native_refs_for_event(
                event.event_id,
            )
            assert len(native_refs) >= 1

            # Assert: original failed receipt excluded from next due query
            due_after = await temp_storage.list_due_retry_receipts(future_now)
            original_ids = {r.receipt_id for r in due_after}
            assert original_receipt.receipt_id not in original_ids

            # Verify receipts: 1 original failed + 1 retry success
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            failed_rcpts = [r for r in all_receipts if r.status == "failed"]
            sent_rcpts = [r for r in all_receipts if r.status == "sent"]
            assert len(failed_rcpts) == 1
            assert len(sent_rcpts) >= 1
            retry_sent = [
                r
                for r in sent_rcpts
                if r.parent_receipt_id == original_receipt.receipt_id
            ]
            assert len(retry_sent) == 1
            assert retry_sent[0].attempt_number == 2
        finally:
            await runner.stop()

    # ------------------------------------------------------------------
    # Route/config change semantics
    # ------------------------------------------------------------------

    async def test_retry_after_adapter_removed(self, temp_storage):
        """When adapter is removed between runs, retry produces ADAPTER_MISSING
        failure — no crash."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="target-a",
            fail_count=999,
        )
        event = _make_event()
        route = Route(
            id="route-a",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="target-a")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"target-a": adapter}

        retry_policy = RetryPolicy(max_attempts=5)
        resolver = _FallbackResolverWithRetry(retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            # First delivery: transient failure
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            original = [r for r in receipts if r.status == "failed"][0]
            assert original.failure_kind == "adapter_transient"
            # Verify retry policy persisted
            assert original.retry_max_attempts == 5
        finally:
            await runner.stop()

        # Second runtime WITHOUT adapter "target-a"
        adapters_b: dict = {}
        runner_b = _build_runner(
            temp_storage,
            adapters_b,
            router,
            RuntimeAccounting(),
            fallback_resolver=resolver,
        )
        await runner_b.start()
        try:
            failed = [
                r
                for r in await temp_storage.list_receipts_for_event(event.event_id)
                if r.status == "failed"
            ]
            assert len(failed) >= 1
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=failed,
            )

            policy = RetryPolicy(max_attempts=5)
            worker = _RetryWorker(
                temp_storage,
                runner_b,
                policy,
                accounting=RuntimeAccounting(),
            )
            # This should NOT crash despite adapter missing
            processed = await worker._process_due(datetime.now(timezone.utc))
            assert processed == 1
            assert worker.state.failed == 1

            # Verify an ADAPTER_MISSING receipt was created
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            adapter_missing = [
                r for r in all_receipts if r.failure_kind == "adapter_missing"
            ]
            assert len(adapter_missing) >= 1
        finally:
            await runner_b.stop()

    async def test_retry_preserves_target_channel_after_route_change(
        self, temp_storage
    ):
        """Retry uses stored target_channel, not current route config."""
        adapter = _TransientThenSucceedAdapter(
            adapter_id="target-a",
            fail_count=1,
        )
        event = _make_event()
        original_channel = "!room:1"
        route = Route(
            id="channel-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="target-a", channel=original_channel)],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"target-a": adapter}

        retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            original = [r for r in receipts if r.status == "failed"][0]
            assert original.target_channel == original_channel
            assert original.retry_max_attempts == 3
        finally:
            await runner.stop()

        # Second runtime with route changed to a different channel
        new_channel = "!room:2"
        changed_route = Route(
            id="channel-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="target-a", channel=new_channel)],
        )
        router_b = Router(routes=[changed_route])
        adapter_b = FakePresentationAdapter(adapter_id="target-a")
        adapters_b = {"target-a": adapter_b}
        runner_b = _build_runner(
            temp_storage,
            adapters_b,
            router_b,
            RuntimeAccounting(),
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters_b)
        await runner_b.start()

        try:
            failed = [
                r
                for r in await temp_storage.list_receipts_for_event(event.event_id)
                if r.status == "failed"
            ]
            assert len(failed) >= 1
            temp_storage.list_due_retry_receipts = AsyncMock(
                return_value=failed,
            )

            policy = RetryPolicy(max_attempts=5)
            worker = _RetryWorker(
                temp_storage,
                runner_b,
                policy,
                accounting=RuntimeAccounting(),
            )
            processed = await worker._process_due(datetime.now(timezone.utc))
            assert processed == 1
            assert worker.state.succeeded == 1

            # Verify retry used stored target_channel, not route's new one
            all_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            retry_receipts = [
                r for r in all_receipts if r.parent_receipt_id == original.receipt_id
            ]
            assert len(retry_receipts) >= 1
            assert retry_receipts[0].target_channel == original_channel
            assert retry_receipts[0].target_channel != new_channel
        finally:
            await runner_b.stop()

    async def test_retry_policy_reconstructed_from_receipt(self, temp_storage):
        """RetryWorker reconstructs RetryPolicy from receipt metadata,
        not from its own defaults."""
        custom_policy = RetryPolicy(
            max_attempts=7,
            backoff_base=5.0,
            max_delay_seconds=120.0,
            jitter=False,
        )
        adapter = _TransientThenSucceedAdapter(
            adapter_id="policy_target",
            fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="policy-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="policy_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"policy_target": adapter}

        resolver = _FallbackResolverWithRetry(custom_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            original = [r for r in receipts if r.status == "failed"][0]

            # Verify custom policy persisted on receipt
            assert original.retry_max_attempts == 7
            assert original.retry_backoff_base == 5.0
            assert original.retry_max_delay == 120.0
            assert original.retry_jitter is False

            # Retry using the real RetryWorker with DIFFERENT defaults
            from medre.config.model import RuntimeLimits
            from medre.core.supervision.capacity import CapacityController
            from medre.runtime.retry import RetryWorker

            limits = RuntimeLimits(
                max_inflight_deliveries=10,
                max_inflight_replay_events=10,
                shutdown_drain_timeout_seconds=5,
                delivery_acquire_timeout_seconds=0.5,
            )
            capacity = CapacityController(limits)
            # max_attempts=3 is DIFFERENT from the stored 7
            worker = RetryWorker(
                temp_storage,
                runner,
                capacity,
                enabled=True,
                interval_seconds=10.0,
                batch_size=20,
                max_attempts=3,
            )

            future_now = original.next_retry_at + timedelta(seconds=1)
            await worker._process_due(future_now)

            assert worker.state.succeeded == 1
            assert worker.state.processed == 1
        finally:
            await runner.stop()

    # ------------------------------------------------------------------
    # Full operator-visible workflow
    # ------------------------------------------------------------------

    async def test_retry_operator_workflow(self, temp_storage):
        """Full operator-visible retry workflow exercising the complete
        operator story: inject → transient failure → trace → retry → trace
        again → recover (no pending) → evidence → inspect → snapshot.

        Proves trace/recover/evidence consistency across the retry lifecycle.
        """
        from medre.config.model import RuntimeLimits
        from medre.core.supervision.capacity import CapacityController
        from medre.runtime.retry import RetryWorker
        from medre.runtime.timeline import (
            assemble_event_timeline,
            assemble_storage_summary,
        )

        # --- 1. Bridge setup: fake matrix source → meshtastic target ---
        adapter = _TransientThenSucceedAdapter(
            adapter_id="mesh_target",
            fail_count=1,
        )
        event = _make_event()
        route = Route(
            id="mx_to_mesh",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="mesh_target", channel="1")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"mesh_target": adapter}

        retry_policy = RetryPolicy(max_attempts=3)
        resolver = _FallbackResolverWithRetry(retry_policy)
        runner = _build_runner(
            temp_storage,
            adapters,
            router,
            accounting,
            fallback_resolver=resolver,
        )
        await _start_adapters(adapters)
        await runner.start()

        try:
            # --- 2. Inject event → transient failure ---
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # --- 3. Get failed receipt from storage ---
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original_receipt = failed[0]
            assert original_receipt.failure_kind == "adapter_transient"
            assert original_receipt.next_retry_at is not None
            assert original_receipt.attempt_number == 1
            assert original_receipt.source == "live"

            # --- 4. Trace shows the failed receipt ---
            timeline = await assemble_event_timeline(
                temp_storage,
                event.event_id,
            )
            assert timeline is not None
            assert len(timeline["receipts"]) == 1
            assert timeline["receipts"][0].status == "failed"
            assert timeline["source"] == "live"

            # --- 5. RetryWorker picks up due receipt and succeeds ---
            future_now = original_receipt.next_retry_at + timedelta(seconds=1)

            limits = RuntimeLimits(
                max_inflight_deliveries=10,
                max_inflight_replay_events=10,
                shutdown_drain_timeout_seconds=5,
                delivery_acquire_timeout_seconds=0.5,
            )
            capacity = CapacityController(limits)
            worker = RetryWorker(
                temp_storage,
                runner,
                capacity,
                enabled=True,
                interval_seconds=10.0,
                batch_size=20,
                max_attempts=3,
            )
            await worker._process_due(future_now)

            # --- 6. Retry succeeded ---
            assert worker.state.succeeded == 1
            assert worker.state.processed == 1

            # --- 7. Trace now shows both failed + success receipts ---
            timeline_after = await assemble_event_timeline(
                temp_storage,
                event.event_id,
            )
            assert timeline_after is not None
            all_receipts_tl = timeline_after["receipts"]
            assert len(all_receipts_tl) == 2
            statuses = {r.status for r in all_receipts_tl}
            assert statuses == {"failed", "sent"}

            # Source classification is "mixed" (live failed + retry success)
            assert timeline_after["source"] == "mixed"

            # --- 8. Recover: no retry pending (all resolved) ---
            due_after = await temp_storage.list_due_retry_receipts(future_now)
            assert len(due_after) == 0, "No retry should be pending — all resolved"

            # --- 9. Evidence: storage summary shows retry lineage ---
            summary = await assemble_storage_summary(temp_storage)
            assert summary["event_count"] == 1
            assert summary["receipt_count"] == 2
            assert summary["receipt_count_by_source"]["live"] == 1
            assert summary["receipt_count_by_source"]["retry"] == 1

            # --- 10. Inspect: receipts show correct sources ---
            stored_receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            sources = {r.source for r in stored_receipts}
            assert sources == {"live", "retry"}

            live_rcpts = [r for r in stored_receipts if r.source == "live"]
            retry_rcpts = [r for r in stored_receipts if r.source == "retry"]
            assert len(live_rcpts) == 1
            assert len(retry_rcpts) == 1

            # Retry receipt linked to original
            assert retry_rcpts[0].parent_receipt_id == original_receipt.receipt_id
            assert retry_rcpts[0].attempt_number == 2

            # --- 11. Final snapshot includes retry counters ---
            assert worker.state.processed == 1
            assert worker.state.succeeded == 1
            assert worker.state.failed == 0
            assert worker.state.dead_lettered == 0

            # Native ref persisted on retry success
            native_refs = await temp_storage.list_native_refs_for_event(
                event.event_id,
            )
            assert len(native_refs) >= 1
        finally:
            await runner.stop()
