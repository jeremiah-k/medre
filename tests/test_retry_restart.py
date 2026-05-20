"""Retry restart tests — verify retry state survives process restart.

Uses persistent SQLite (temp_storage) to simulate stop/start of the
runtime: PipelineRunner A creates a transient failure, then Runner B
opens the same database and successfully retries.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
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
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.observability.classification import infer_failure_kind

# ---------------------------------------------------------------------------
# Custom adapter: fails N times then succeeds
# ---------------------------------------------------------------------------


class _FailsThenSucceedsAdapter(FakePresentationAdapter):
    """Raises ConnectionError for the first N deliveries, then succeeds.

    Tracks call count across instances when given the same counter list.
    """

    def __init__(
        self,
        adapter_id: str = "restart_target",
        fail_count: int = 1,
        call_counter: list | None = None,
    ) -> None:
        super().__init__(adapter_id=adapter_id)
        self._fail_count = fail_count
        self._call_counter = call_counter if call_counter is not None else [0]

    async def deliver(self, result):
        self._call_counter[0] += 1
        if self._call_counter[0] <= self._fail_count:
            raise ConnectionError(
                f"Transient failure #{self._call_counter[0]} from {self.adapter_id}"
            )
        return await super().deliver(result)


# ---------------------------------------------------------------------------
# RetryWorker (restart variant)
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
        payload={"body": "restart test"},
        metadata=EventMetadata(),
    )


class _FallbackResolverWithRetry(FallbackResolver):
    """FallbackResolver that injects a RetryPolicy into every delivery plan."""

    def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
        self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)

    def resolve_fallback(
        self,
        event: CanonicalEvent,
        target: RouteTarget,
        capabilities: dict,
    ) -> DeliveryPlan:
        plan = super().resolve_fallback(event, target, capabilities)
        plan.retry_policy = self._retry_policy
        return plan


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
        fallback_resolver=_FallbackResolverWithRetry(),
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


def _persistent_storage():
    """Create a temp-file SQLite storage (caller must close + unlink)."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = f.name
    f.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryRestart:
    """Retry state survives process restart via persistent SQLite."""

    async def test_retry_across_restart(self):
        """Failed receipt persisted by Runner A is retried by Runner B."""
        db_path = _persistent_storage()
        call_counter = [0]

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="restart-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="restart_target")],
            )
            router = Router(routes=[route])
            accounting_a = RuntimeAccounting()
            adapters_a = {"restart_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting_a)
            await _start_adapters(adapters_a)
            await runner_a.start()

            outcomes = await runner_a.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts_a = await storage_a.list_receipts_for_event(event.event_id)
            original_receipt = [r for r in receipts_a if r.status == "failed"][0]
            assert original_receipt.attempt_number == 1

            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"restart_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()

            try:
                receipts_b = await storage_b.list_receipts_for_event(event.event_id)
                failed_b = [r for r in receipts_b if r.status == "failed"]
                assert len(failed_b) >= 1
                storage_b.list_due_retry_receipts = AsyncMock(return_value=failed_b)

                policy = RetryPolicy(max_attempts=5)
                worker = _RetryWorker(
                    storage_b, runner_b, policy, accounting=accounting_b
                )
                processed = await worker._process_due(datetime.now(timezone.utc))

                assert processed == 1
                assert worker.state.succeeded == 1

                all_receipts = await storage_b.list_receipts_for_event(event.event_id)
                retry = [
                    r
                    for r in all_receipts
                    if r.parent_receipt_id == original_receipt.receipt_id
                ][0]
                assert retry.attempt_number == 2
                assert retry.parent_receipt_id == original_receipt.receipt_id

                native_refs = await storage_b.list_native_refs_for_event(event.event_id)
                assert len(native_refs) >= 1
                assert accounting_b.snapshot()["inbound_accepted"] == 0
            finally:
                await runner_b.stop()
                await storage_b.close()

        finally:
            os.unlink(db_path)

    async def test_no_duplicate_canonical_event_after_retry_restart(self):
        """Retry does not create a new canonical event in storage."""
        db_path = _persistent_storage()
        call_counter = [0]

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="dedup-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="restart_target")],
            )
            router = Router(routes=[route])
            accounting = RuntimeAccounting()
            adapters_a = {"restart_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting)
            await _start_adapters(adapters_a)
            await runner_a.start()

            await runner_a.handle_ingress(event)
            event_count_a = await storage_a.count_events()

            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"restart_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()

            try:
                receipts = await storage_b.list_receipts_for_event(event.event_id)
                failed = [r for r in receipts if r.status == "failed"]
                storage_b.list_due_retry_receipts = AsyncMock(return_value=failed)

                policy = RetryPolicy(max_attempts=5)
                worker = _RetryWorker(
                    storage_b, runner_b, policy, accounting=accounting_b
                )
                await worker._process_due(datetime.now(timezone.utc))

                event_count_b = await storage_b.count_events()
                assert event_count_b == event_count_a
                assert event_count_b == 1
            finally:
                await runner_b.stop()
                await storage_b.close()

        finally:
            os.unlink(db_path)

    async def test_real_failure_retry_across_restart(self):
        """Real pipeline failure creates due receipt; after restart, retry
        succeeds with correct lineage and source."""
        db_path = _persistent_storage()
        call_counter = [0]

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="real-restart-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="restart_target")],
            )
            router = Router(routes=[route])
            accounting_a = RuntimeAccounting()
            adapters_a = {"restart_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting_a)
            await _start_adapters(adapters_a)
            await runner_a.start()

            outcomes = await runner_a.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts_a = await storage_a.list_receipts_for_event(event.event_id)
            original = [r for r in receipts_a if r.status == "failed"][0]
            assert original.attempt_number == 1
            assert original.failure_kind == "adapter_transient"
            assert original.next_retry_at is not None

            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _FailsThenSucceedsAdapter(
                adapter_id="restart_target",
                fail_count=1,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"restart_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()

            try:
                future_now = original.next_retry_at + timedelta(seconds=1)
                due = await storage_b.list_due_retry_receipts(future_now)
                assert len(due) >= 1

                policy = RetryPolicy(max_attempts=5)
                worker = _RetryWorker(
                    storage_b, runner_b, policy, accounting=accounting_b
                )
                processed = await worker._process_due(future_now)
                assert processed == 1
                assert worker.state.succeeded == 1

                all_receipts = await storage_b.list_receipts_for_event(event.event_id)
                orig = [r for r in all_receipts if r.receipt_id == original.receipt_id]
                assert len(orig) == 1
                assert orig[0].status == "failed"
                assert orig[0].attempt_number == 1

                retry = [
                    r
                    for r in all_receipts
                    if r.parent_receipt_id == original.receipt_id
                ][0]
                assert retry.attempt_number == 2
                assert retry.parent_receipt_id == original.receipt_id
                assert retry.source == "retry"

                native_refs = await storage_b.list_native_refs_for_event(event.event_id)
                assert len(native_refs) >= 1
                assert await storage_b.count_events() == 1
            finally:
                await runner_b.stop()
                await storage_b.close()

        finally:
            os.unlink(db_path)

    async def test_retry_across_restart_preserves_target_channel(self):
        """Target channel persisted in receipt is preserved across restart retry."""
        db_path = _persistent_storage()
        call_counter = [0]
        target_ch = "!room:example.com"

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _FailsThenSucceedsAdapter(
                adapter_id="channel_target",
                fail_count=1,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="channel-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="channel_target", channel=target_ch)],
            )
            router = Router(routes=[route])
            accounting_a = RuntimeAccounting()
            adapters_a = {"channel_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting_a)
            await _start_adapters(adapters_a)
            await runner_a.start()
            outcomes = await runner_a.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"
            receipts_a = await storage_a.list_receipts_for_event(event.event_id)
            original = [r for r in receipts_a if r.status == "failed"][0]
            assert original.target_channel == target_ch
            assert original.attempt_number == 1
            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _FailsThenSucceedsAdapter(
                adapter_id="channel_target",
                fail_count=1,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"channel_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()
            try:
                failed_b = [
                    r
                    for r in await storage_b.list_receipts_for_event(event.event_id)
                    if r.status == "failed"
                ]
                assert len(failed_b) >= 1
                assert failed_b[0].target_channel == target_ch
                storage_b.list_due_retry_receipts = AsyncMock(return_value=failed_b)
                policy = RetryPolicy(max_attempts=5)
                worker = _RetryWorker(
                    storage_b,
                    runner_b,
                    policy,
                    accounting=accounting_b,
                )
                processed = await worker._process_due(datetime.now(timezone.utc))
                assert processed == 1
                assert worker.state.succeeded == 1

                all_receipts = await storage_b.list_receipts_for_event(event.event_id)
                retry = [
                    r
                    for r in all_receipts
                    if r.parent_receipt_id == original.receipt_id
                ][0]
                assert retry.attempt_number == 2
                assert retry.parent_receipt_id == original.receipt_id
                assert retry.target_channel == target_ch
                native_refs = await storage_b.list_native_refs_for_event(event.event_id)
                assert len(native_refs) >= 1
            finally:
                await runner_b.stop()
                await storage_b.close()
        finally:
            os.unlink(db_path)

    async def test_restart_dead_letter_preserves_target_channel(self):
        """Dead-letter receipt carries target_channel across restart."""

        db_path = _persistent_storage()
        call_counter = [0]
        target_ch = "#mesh:example.com"

        class _AlwaysTransientAdapter(_FailsThenSucceedsAdapter):
            """Always raises ConnectionError regardless of call count."""

            async def deliver(self, result):
                raise ConnectionError("permanent transient for dead-letter test")

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _AlwaysTransientAdapter(
                adapter_id="dl_target",
                fail_count=999,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="dl-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="dl_target", channel=target_ch)],
            )
            router = Router(routes=[route])
            accounting_a = RuntimeAccounting()
            adapters_a = {"dl_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting_a)
            await _start_adapters(adapters_a)
            await runner_a.start()
            outcomes = await runner_a.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"
            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _AlwaysTransientAdapter(
                adapter_id="dl_target",
                fail_count=999,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"dl_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()
            try:
                # Verify the original failed receipt has target_channel
                receipts = await storage_b.list_receipts_for_event(event.event_id)
                original = [r for r in receipts if r.status == "failed"][0]
                assert original.target_channel == target_ch
                assert original.failure_kind == "adapter_transient"

                # Retry via worker — will fail again, exhausting retries
                policy = RetryPolicy(max_attempts=1)
                worker = _RetryWorker(
                    storage_b,
                    runner_b,
                    policy,
                    accounting=accounting_b,
                )
                storage_b.list_due_retry_receipts = AsyncMock(
                    return_value=[original],
                )
                processed = await worker._process_due(datetime.now(timezone.utc))
                assert processed == 1
                assert worker.state.dead_lettered == 1

                # Verify dead-letter receipt has target_channel
                all_receipts = await storage_b.list_receipts_for_event(
                    event.event_id,
                )
                dead = [r for r in all_receipts if r.status == "dead_lettered"]
                assert len(dead) >= 1
                assert dead[0].target_channel == target_ch

                # Verify NO native ref persisted (never succeeded)
                native_refs = await storage_b.list_native_refs_for_event(
                    event.event_id,
                )
                assert len(native_refs) == 0

                # Verify accounting from runner B is clean (no inbound)
                snap = accounting_b.snapshot()
                assert snap["inbound_accepted"] == 0
            finally:
                await runner_b.stop()
                await storage_b.close()
        finally:
            os.unlink(db_path)

    async def test_restart_original_excluded_after_retry_child(self):
        """After retry produces a child receipt, the original is excluded
        from subsequent due-receipt queries (NOT EXISTS guard)."""
        db_path = _persistent_storage()
        call_counter = [0]
        target_ch = "!room:restart.org"

        try:
            storage_a = SQLiteStorage(db_path=db_path)
            await storage_a.initialize()
            adapter_a = _FailsThenSucceedsAdapter(
                adapter_id="excl_target",
                fail_count=1,
                call_counter=call_counter,
            )
            event = _make_event()
            route = Route(
                id="excl-route",
                source=RouteSource(
                    adapter="fake_source",
                    event_kinds=("message.created",),
                    channel=None,
                ),
                targets=[RouteTarget(adapter="excl_target", channel=target_ch)],
            )
            router = Router(routes=[route])
            accounting_a = RuntimeAccounting()
            adapters_a = {"excl_target": adapter_a}
            runner_a = _build_runner(storage_a, adapters_a, router, accounting_a)
            await _start_adapters(adapters_a)
            await runner_a.start()
            outcomes = await runner_a.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts_a = await storage_a.list_receipts_for_event(event.event_id)
            original = [r for r in receipts_a if r.status == "failed"][0]
            assert original.target_channel == target_ch
            await runner_a.stop()
            await storage_a.close()

            storage_b = SQLiteStorage(db_path=db_path)
            await storage_b.initialize()
            adapter_b = _FailsThenSucceedsAdapter(
                adapter_id="excl_target",
                fail_count=1,
                call_counter=call_counter,
            )
            accounting_b = RuntimeAccounting()
            adapters_b = {"excl_target": adapter_b}
            runner_b = _build_runner(storage_b, adapters_b, router, accounting_b)
            await _start_adapters(adapters_b)
            await runner_b.start()
            try:
                # Retry succeeds using real storage query
                future_now = original.next_retry_at + timedelta(seconds=1)
                due = await storage_b.list_due_retry_receipts(future_now)
                assert len(due) >= 1

                policy = RetryPolicy(max_attempts=5)
                worker = _RetryWorker(
                    storage_b,
                    runner_b,
                    policy,
                    accounting=accounting_b,
                )
                processed = await worker._process_due(future_now)
                assert processed == 1
                assert worker.state.succeeded == 1

                # Now the original should be excluded from due queries
                due_after = await storage_b.list_due_retry_receipts(future_now)
                original_ids = {r.receipt_id for r in due_after}
                assert original.receipt_id not in original_ids

                # Verify native ref persisted only on retry success
                native_refs = await storage_b.list_native_refs_for_event(
                    event.event_id,
                )
                assert len(native_refs) >= 1

                # Verify target_channel preserved on retry receipt
                all_receipts = await storage_b.list_receipts_for_event(
                    event.event_id,
                )
                retry = [
                    r
                    for r in all_receipts
                    if r.parent_receipt_id == original.receipt_id
                ]
                assert len(retry) >= 1
                assert retry[0].target_channel == target_ch
            finally:
                await runner_b.stop()
                await storage_b.close()
        finally:
            os.unlink(db_path)
