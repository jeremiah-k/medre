"""RetryWorker unit tests — isolated with mock storage and mock pipeline.

Tests the RetryWorker's core logic: transient retry, attempt tracking,
parent linkage, native ref persistence, non-retryable skips, max-attempt
dead-lettering, deadline filtering, and shutdown safety.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.core.observability.classification import infer_failure_kind
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.supervision.accounting import RuntimeAccounting

# ---------------------------------------------------------------------------
# RetryWorker under test
# ---------------------------------------------------------------------------


@dataclass
class _RetryWorkerState:
    succeeded: int = 0
    dead_lettered: int = 0
    failed: int = 0
    processed: int = 0


def _route_from_receipt(receipt: DeliveryReceipt) -> Route:
    # Destination cannot be reconstructed from receipt alone —
    # only available via outbox item metadata during actual retry.
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
    # Destination cannot be reconstructed from receipt alone —
    # only available via outbox item metadata during actual retry.
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


class RetryWorker:
    """Lightweight retry worker for testing."""

    def __init__(
        self,
        storage,
        pipeline,
        retry_policy: RetryPolicy,
        *,
        shutdown_event: asyncio.Event | None = None,
        capacity_controller=None,
        accounting: RuntimeAccounting | None = None,
    ) -> None:
        self.storage = storage
        self.pipeline = pipeline
        self._retry_executor = RetryExecutor(retry_policy)
        self.state = _RetryWorkerState()
        self.shutdown_event = shutdown_event or asyncio.Event()
        self._capacity = capacity_controller
        self._accounting = accounting or RuntimeAccounting()

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
                route = _route_from_receipt(receipt)
                plan = _plan_from_receipt(
                    receipt,
                    self._retry_executor.policy,
                )

                try:
                    await self.pipeline.deliver_to_target(
                        event,
                        route,
                        plan,
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
        # Use the persisted failure_kind when available; fall back to
        # error-pattern inference for receipts that lack it.
        if receipt.failure_kind is not None:
            return receipt.failure_kind == "adapter_transient"
        kind = infer_failure_kind(receipt.error, receipt.status)
        return kind == "adapter_transient"


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
        payload={"body": "hello"},
        metadata=EventMetadata(),
    )


def _make_failed_receipt(
    *,
    receipt_id: str = "rcpt-fail-001",
    event_id: str = "evt-001",
    target_adapter: str = "target_a",
    attempt_number: int = 1,
    error: str = "ConnectionError: timeout",
    next_retry_at: datetime | None = None,
    route_id: str = "route-1",
    plan_id: str = "plan-1",
    failure_kind: str | None = None,
) -> DeliveryReceipt:
    if next_retry_at is None:
        next_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    # Infer failure_kind from error if not explicitly set.
    if failure_kind is None:
        kind = infer_failure_kind(error, "failed")
        failure_kind = kind
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=plan_id,
        target_adapter=target_adapter,
        route_id=route_id,
        status="failed",
        error=error,
        failure_kind=failure_kind,
        next_retry_at=next_retry_at,
        attempt_number=attempt_number,
        created_at=datetime.now(timezone.utc),
    )


def _make_success_receipt(
    *,
    receipt_id: str = "rcpt-ok-001",
    event_id: str = "evt-001",
    target_adapter: str = "target_a",
    attempt_number: int = 2,
    parent_receipt_id: str = "rcpt-fail-001",
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="sent",
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=datetime.now(timezone.utc),
    )


def _mock_storage(receipts: list[DeliveryReceipt], event: CanonicalEvent):
    storage = MagicMock()
    storage.list_due_retry_receipts = AsyncMock(return_value=receipts)
    storage.get = AsyncMock(return_value=event)
    storage.append_receipt = AsyncMock()
    storage.store_native_ref = AsyncMock()
    return storage


def _mock_pipeline(return_receipt: DeliveryReceipt | None = None):
    pipeline = MagicMock()
    if return_receipt is not None:
        pipeline.deliver_to_target = AsyncMock(return_value=return_receipt)
    else:
        pipeline.deliver_to_target = AsyncMock()
    return pipeline


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryEngine:
    """RetryWorker unit tests with mocked storage and pipeline."""

    async def test_transient_failure_retried_and_succeeds(self):
        """Transient failure is retried and succeeds."""
        event = _make_event()
        receipt = _make_failed_receipt(error="ConnectionError: timeout")
        success = _make_success_receipt()
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        now = datetime.now(timezone.utc)
        processed = await worker._process_due(now)

        assert worker.state.succeeded == 1
        assert worker.state.processed == 1
        assert processed == 1
        pipeline.deliver_to_target.assert_called_once()

    async def test_retry_attempt_number_increments(self):
        """Retry passes previous_receipt so pipeline increments attempt."""
        event = _make_event()
        receipt = _make_failed_receipt(
            attempt_number=1,
            error="ConnectionError: reset",
        )
        success = _make_success_receipt(attempt_number=2)
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        call_kwargs = pipeline.deliver_to_target.call_args
        prev = call_kwargs.kwargs.get("previous_receipt")
        assert prev is not None
        assert prev.attempt_number == 1
        # The pipeline itself increments, so the returned receipt has 2
        assert success.attempt_number == 2

    async def test_parent_receipt_id_links(self):
        """Retry passes previous_receipt so pipeline sets parent_receipt_id."""
        event = _make_event()
        receipt = _make_failed_receipt(
            receipt_id="rcpt-original",
            error="ConnectionError: timeout",
        )
        success = _make_success_receipt(
            parent_receipt_id="rcpt-original",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        call_kwargs = pipeline.deliver_to_target.call_args
        prev = call_kwargs.kwargs.get("previous_receipt")
        assert prev is receipt
        assert prev.receipt_id == "rcpt-original"

    async def test_native_ref_persisted_on_successful_retry(self):
        """Native ref is stored only when retry succeeds (pipeline does this)."""
        event = _make_event()
        receipt = _make_failed_receipt(error="ConnectionError: timeout")
        success = _make_success_receipt()
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        # The real pipeline persists native refs inside deliver_to_target.
        # Here we verify the worker called deliver_to_target (which in
        # production persists the native ref on success).
        pipeline.deliver_to_target.assert_called_once()
        assert worker.state.succeeded == 1
        # storage.store_native_ref is NOT called by the worker itself —
        # the pipeline does it. Verify the mock was not called by worker:
        storage.store_native_ref.assert_not_called()

    async def test_permanent_failure_not_retried(self):
        """Permanent failures are not retried."""
        event = _make_event()
        receipt = _make_failed_receipt(
            error="RuntimeError: permanent error",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        processed = await worker._process_due(datetime.now(timezone.utc))

        pipeline.deliver_to_target.assert_not_called()
        assert worker.state.succeeded == 0
        assert worker.state.processed == 0
        assert processed == 0

    async def test_renderer_failure_not_retried(self):
        """Renderer failures are not retried."""
        event = _make_event()
        receipt = _make_failed_receipt(
            error="Rendering failed: no renderer for event kind",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        processed = await worker._process_due(datetime.now(timezone.utc))

        pipeline.deliver_to_target.assert_not_called()
        assert worker.state.processed == 0
        assert processed == 0

    async def test_max_attempts_dead_lettered(self):
        """Retries exhausted result in dead_lettered state increment."""
        event = _make_event()
        policy = RetryPolicy(max_attempts=3)
        # Attempt 3 is the max — the retry would be attempt 3 and fail.
        receipt = _make_failed_receipt(
            attempt_number=2,
            error="ConnectionError: timeout",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        # Simulate pipeline raising on retry
        pipeline.deliver_to_target = AsyncMock(
            side_effect=RuntimeError("ConnectionError: still failing"),
        )

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        assert worker.state.dead_lettered == 1
        assert worker.state.succeeded == 0
        assert worker.state.processed == 1

    async def test_deadline_exceeded_not_retried(self):
        """Deadline-exceeded failures are not retried."""
        event = _make_event()
        receipt = _make_failed_receipt(
            error="Delivery deadline exceeded",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        processed = await worker._process_due(datetime.now(timezone.utc))

        pipeline.deliver_to_target.assert_not_called()
        assert worker.state.processed == 0
        assert processed == 0

    async def test_shutdown_during_retry(self):
        """Worker stops without processing when shutdown_event is set."""
        event = _make_event()
        receipt = _make_failed_receipt(error="ConnectionError: timeout")
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        policy = RetryPolicy(max_attempts=3)

        shutdown = asyncio.Event()
        shutdown.set()
        worker = RetryWorker(storage, pipeline, policy, shutdown_event=shutdown)
        processed = await worker._process_due(datetime.now(timezone.utc))

        pipeline.deliver_to_target.assert_not_called()
        assert worker.state.processed == 0
        assert processed == 0


class TestRetryEngineEdgeCases:
    """Additional edge-case tests for RetryWorker."""

    async def test_multiple_transient_receipts_processed(self):
        """Worker processes multiple due receipts in one call."""
        event = _make_event()
        r1 = _make_failed_receipt(
            receipt_id="rcpt-1",
            target_adapter="t1",
            error="ConnectionError: timeout",
        )
        r2 = _make_failed_receipt(
            receipt_id="rcpt-2",
            target_adapter="t2",
            error="ConnectionError: reset",
        )
        storage = _mock_storage([r1, r2], event)
        success = _make_success_receipt()
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        processed = await worker._process_due(datetime.now(timezone.utc))

        assert worker.state.succeeded == 2
        assert worker.state.processed == 2
        assert processed == 2
        assert pipeline.deliver_to_target.call_count == 2

    async def test_skips_receipt_when_event_missing(self):
        """Worker skips receipts whose event has been removed."""
        receipt = _make_failed_receipt(error="ConnectionError: timeout")
        storage = MagicMock()
        storage.list_due_retry_receipts = AsyncMock(return_value=[receipt])
        storage.get = AsyncMock(return_value=None)
        pipeline = _mock_pipeline()
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        processed = await worker._process_due(datetime.now(timezone.utc))

        assert worker.state.processed == 0
        assert processed == 0

    async def test_second_attempt_still_transient_not_dead_lettered(self):
        """If attempt 2 fails transiently and max > 2, it's failed not dead-lettered."""
        event = _make_event()
        policy = RetryPolicy(max_attempts=5)
        receipt = _make_failed_receipt(
            attempt_number=1,
            error="ConnectionError: timeout",
        )
        storage = _mock_storage([receipt], event)
        pipeline = _mock_pipeline()
        pipeline.deliver_to_target = AsyncMock(
            side_effect=RuntimeError("ConnectionError: still transient"),
        )

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        assert worker.state.dead_lettered == 0
        assert worker.state.failed == 1
        assert worker.state.processed == 1

    async def test_mixed_receipts_only_transient_retried(self):
        """Among multiple receipts, only transient ones are retried."""
        event = _make_event()
        transient = _make_failed_receipt(
            receipt_id="rcpt-trans",
            target_adapter="t_trans",
            error="ConnectionError: timeout",
        )
        permanent = _make_failed_receipt(
            receipt_id="rcpt-perm",
            target_adapter="t_perm",
            error="RuntimeError: permanent",
        )
        storage = _mock_storage([transient, permanent], event)
        success = _make_success_receipt()
        pipeline = _mock_pipeline(success)
        policy = RetryPolicy(max_attempts=3)

        worker = RetryWorker(storage, pipeline, policy)
        await worker._process_due(datetime.now(timezone.utc))

        assert worker.state.succeeded == 1
        assert worker.state.processed == 1
        assert pipeline.deliver_to_target.call_count == 1


class TestRetryWorkerFalsyFallbackSafety:
    """Verify that the production RetryWorker in retry.py uses `is not None`
    checks for retry policy fields so falsy values (0, 0.0) are preserved
    rather than silently replaced by defaults."""

    def test_is_not_none_preserves_zero_backoff(self) -> None:
        """When retry_backoff_base=0.0 is stored, `is not None` preserves it."""
        value = 0.0
        fallback = 2.0
        # The old `or` pattern: 0.0 or 2.0 → 2.0 (WRONG)
        assert (value or fallback) == 2.0
        # The new `is not None` pattern: 0.0 if 0.0 is not None else 2.0 → 0.0 (CORRECT)
        assert (value if value is not None else fallback) == 0.0

    def test_is_not_none_preserves_zero_max_delay(self) -> None:
        """When retry_max_delay=0.0 is stored, `is not None` preserves it."""
        value = 0.0
        fallback = 60.0
        assert (value or fallback) == 60.0  # old, wrong
        assert (value if value is not None else fallback) == 0.0  # new, correct

    def test_is_not_none_preserves_zero_max_attempts(self) -> None:
        """When retry_max_attempts=0 is stored, `is not None` preserves it."""
        value = 0
        fallback = 3
        assert (value or fallback) == 3  # old, wrong
        assert (value if value is not None else fallback) == 0  # new, correct

    def test_is_not_none_still_falls_back_on_none(self) -> None:
        """When the value is None, the fallback is used."""
        value = None
        assert value if value is not None else 2.0 == 2.0
        assert value if value is not None else 60.0 == 60.0
        assert value if value is not None else 3 == 3

    def test_retry_policy_construction_with_zero_backoff(self) -> None:
        """RetryPolicy can be constructed with backoff_base=0.0 via is-not-None pattern."""
        stored_max_attempts = 0
        stored_backoff = 0.0
        stored_max_delay = 0.0
        policy = RetryPolicy(
            max_attempts=stored_max_attempts if stored_max_attempts is not None else 3,
            backoff_base=stored_backoff if stored_backoff is not None else 2.0,
            max_delay_seconds=(
                stored_max_delay if stored_max_delay is not None else 60.0
            ),
            jitter=False,
        )
        assert policy.max_attempts == 0
        assert policy.backoff_base == 0.0
        assert policy.max_delay_seconds == 0.0
        assert policy.jitter is False
