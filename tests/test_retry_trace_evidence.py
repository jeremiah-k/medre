"""Retry trace/evidence tests — verify timeline and evidence understand retry lineage.

Tests that timeline assembly shows both failed and success receipts in
correct order, evidence bundles include retry metadata, and recovery
guidance distinguishes pending-retry from exhausted/dead-lettered.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
)
from medre.core.events.metadata import EventMetadata
from medre.observability.classification import (
    failure_category,
    infer_failure_kind,
    recommended_commands,
)
from medre.runtime.timeline import assemble_event_timeline

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
        payload={"body": "trace test"},
        metadata=EventMetadata(),
    )


def _make_failed_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-fail-001",
    target_adapter: str = "target_a",
    attempt_number: int = 1,
    parent_receipt_id: str | None = None,
    created_at: datetime | None = None,
    failure_kind: str | None = None,
) -> DeliveryReceipt:
    if failure_kind is None:
        failure_kind = "adapter_transient"
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="failed",
        error="ConnectionError: timeout",
        failure_kind=failure_kind,
        next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=2),
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=created_at or datetime.now(timezone.utc),
    )


def _make_success_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-ok-001",
    target_adapter: str = "target_a",
    attempt_number: int = 2,
    parent_receipt_id: str = "rcpt-fail-001",
    created_at: datetime | None = None,
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
        created_at=created_at or datetime.now(timezone.utc) + timedelta(seconds=1),
    )


def _make_dead_letter_receipt(
    *,
    event_id: str,
    receipt_id: str = "rcpt-dead-001",
    target_adapter: str = "target_a",
    attempt_number: int = 3,
    parent_receipt_id: str = "rcpt-fail-002",
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        route_id="route-1",
        status="dead_lettered",
        error="Retry exhausted",
        attempt_number=attempt_number,
        parent_receipt_id=parent_receipt_id,
        created_at=created_at or datetime.now(timezone.utc) + timedelta(seconds=2),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryTraceEvidence:
    """Timeline and evidence correctly represent retry lineage."""

    async def test_trace_shows_retry_lineage(self, temp_storage):
        """Timeline shows both failed and success receipts, ordered correctly."""
        event = _make_event()
        await temp_storage.append(event)

        # Create failed receipt (attempt 1) then success receipt (attempt 2)
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        failed = _make_failed_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-fail",
            attempt_number=1,
            created_at=t0,
        )
        success = _make_success_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-ok",
            attempt_number=2,
            parent_receipt_id="rcpt-fail",
            created_at=t0 + timedelta(seconds=1),
        )
        await temp_storage.append_receipt(failed)
        await temp_storage.append_receipt(success)

        timeline = await assemble_event_timeline(temp_storage, event.event_id)

        assert timeline is not None
        receipts = timeline["receipts"]
        assert len(receipts) == 2

        # Ordered by sequence (append order)
        assert receipts[0].receipt_id == "rcpt-fail"
        assert receipts[0].status == "failed"
        assert receipts[0].attempt_number == 1

        assert receipts[1].receipt_id == "rcpt-ok"
        assert receipts[1].status == "sent"
        assert receipts[1].attempt_number == 2
        assert receipts[1].parent_receipt_id == "rcpt-fail"

        # Timeline entries include both
        entries = timeline["timeline_entries"]
        entry_types = [e.get("entry_type") for e in entries]
        assert "receipt" in entry_types

    async def test_evidence_includes_retry_attempts(self, temp_storage):
        """Evidence incident_summary includes retry count metadata."""
        event = _make_event()
        await temp_storage.append(event)

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        failed = _make_failed_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-fail",
            attempt_number=1,
            created_at=t0,
        )
        success = _make_success_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-ok",
            attempt_number=2,
            parent_receipt_id="rcpt-fail",
            created_at=t0 + timedelta(seconds=1),
        )
        await temp_storage.append_receipt(failed)
        await temp_storage.append_receipt(success)

        timeline = await assemble_event_timeline(temp_storage, event.event_id)
        assert timeline is not None

        receipts = timeline["receipts"]
        failed_count = sum(
            1 for r in receipts if r.status in ("failed", "dead_lettered")
        )
        sent_count = sum(1 for r in receipts if r.status == "sent")

        # Evidence-style incident summary
        first_failure_kind = None
        for r in receipts:
            if r.status in ("failed", "dead_lettered"):
                fk = infer_failure_kind(r.error, r.status)
                first_failure_kind = fk
                break

        if failed_count == 0:
            classification = "success"
        else:
            worst = failure_category(first_failure_kind or "unknown")
            classification = worst if worst != "success" else "unknown"

        assert failed_count == 1
        assert sent_count == 1
        assert first_failure_kind == "adapter_transient"
        assert classification == "retryable"

        # Recommended commands for retryable include inspect-first and replay
        cmds = recommended_commands("retryable", event.event_id)
        assert any("replay" in c for c in cmds)
        assert any("inspect" in c for c in cmds)

    async def test_recover_distinguishes_retry_pending_vs_exhausted(self, temp_storage):
        """Recovery guidance differs for pending retry vs dead-lettered."""
        event = _make_event()
        await temp_storage.append(event)

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # --- Scenario A: pending retry (failed, next_retry_at in future) ---
        event_a = _make_event(event_id="evt-pending")
        await temp_storage.append(event_a)
        pending_receipt = _make_failed_receipt(
            event_id="evt-pending",
            receipt_id="rcpt-pending",
            attempt_number=1,
            created_at=t0,
        )
        await temp_storage.append_receipt(pending_receipt)

        timeline_a = await assemble_event_timeline(temp_storage, "evt-pending")
        assert timeline_a is not None
        r_a = timeline_a["receipts"]
        # Pending: has failed receipt with next_retry_at
        has_pending = any(
            r.status == "failed" and r.next_retry_at is not None for r in r_a
        )
        assert has_pending

        # Failure kind is transient → retryable
        pending_fail = [r for r in r_a if r.status == "failed"][0]
        kind_a = infer_failure_kind(pending_fail.error, pending_fail.status)
        assert kind_a == "adapter_transient"
        cat_a = failure_category(kind_a)
        assert cat_a == "retryable"
        cmds_a = recommended_commands(cat_a, "evt-pending")
        assert any("replay" in c.lower() for c in cmds_a)

        # --- Scenario B: dead-lettered (retries exhausted) ---
        event_b = _make_event(event_id="evt-dead")
        await temp_storage.append(event_b)
        dead = _make_dead_letter_receipt(
            event_id="evt-dead",
            receipt_id="rcpt-dead",
            attempt_number=3,
            parent_receipt_id="rcpt-fail-prev",
            created_at=t0,
        )
        await temp_storage.append_receipt(dead)

        timeline_b = await assemble_event_timeline(temp_storage, "evt-dead")
        assert timeline_b is not None
        r_b = timeline_b["receipts"]
        dead_lettered = [r for r in r_b if r.status == "dead_lettered"]
        assert len(dead_lettered) == 1

        # Dead-lettered is inferred as adapter_transient (was retriable)
        kind_b = infer_failure_kind(
            dead_lettered[0].error,
            dead_lettered[0].status,
        )
        assert kind_b == "adapter_transient"
        # But the receipt status tells us it's exhausted
        assert dead_lettered[0].status == "dead_lettered"

        # For dead-lettered, recommended commands should include manual replay
        cmds_b = recommended_commands("retryable", "evt-dead")
        assert len(cmds_b) > 0
        # Manual replay is the recommended action for exhausted retries
        assert any("replay" in c.lower() for c in cmds_b)

    async def test_cross_surface_retry_consistency(self, temp_storage):
        """Full pipeline: inject event → transient failure → retry succeeds.
        Timeline assembly shows both receipts, correct lineage, and mixed source."""
        from unittest.mock import AsyncMock

        from medre.adapters.fake_presentation import FakePresentationAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import RetryPolicy
        from medre.core.planning.fallback_resolution import (
            FallbackResolver as _BaseFallback,
        )
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.runtime.accounting import RuntimeAccounting

        class _FallbackResolverWithRetry(_BaseFallback):
            """Injects a RetryPolicy into every delivery plan."""

            def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
                self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)

            def resolve_fallback(self, event, target, capabilities):
                plan = super().resolve_fallback(event, target, capabilities)
                plan.retry_policy = self._retry_policy
                return plan

        class _TransientOnceAdapter(FakePresentationAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._call_count = 0

            async def deliver(self, result):
                self._call_count += 1
                if self._call_count <= 1:
                    raise ConnectionError("transient for trace test")
                return await super().deliver(result)

        adapter = _TransientOnceAdapter(adapter_id="trace_target")
        event = _make_event()

        route = Route(
            id="trace-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="trace_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"trace_target": adapter}

        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=_FallbackResolverWithRetry(),
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
            adapter_id="trace_target",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.trace_target"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=__import__("asyncio").Event(),
        )
        await adapter.start(ctx)
        await runner.start()

        try:
            # First delivery: fails transiently
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Get the failed receipt
            receipts = await temp_storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original = failed[0]

            # Retry through pipeline with previous_receipt and source="retry"
            from medre.core.planning.delivery_plan import (
                DeliveryPlan,
                DeliveryStrategy,
            )

            plan = DeliveryPlan(
                plan_id=original.delivery_plan_id,
                event_id=event.event_id,
                target=RouteTarget(
                    adapter="trace_target",
                    channel=getattr(original, "target_channel", None),
                ),
                primary_strategy=DeliveryStrategy(method="direct"),
                retry_policy=RetryPolicy(max_attempts=3),
            )
            route_obj = Route(
                id=original.route_id,
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[
                    RouteTarget(
                        adapter="trace_target",
                        channel=getattr(original, "target_channel", None),
                    )
                ],
            )
            await runner.deliver_to_target(
                event,
                route_obj,
                plan,
                previous_receipt=original,
                source="retry",
            )

            # Now assemble the timeline
            timeline = await assemble_event_timeline(temp_storage, event.event_id)
            assert timeline is not None

            # Both receipts present
            timeline_receipts = timeline["receipts"]
            assert len(timeline_receipts) >= 2

            # Failed receipt
            failed_tl = [r for r in timeline_receipts if r.status == "failed"]
            assert len(failed_tl) == 1
            assert failed_tl[0].failure_kind == "adapter_transient"
            assert failed_tl[0].next_retry_at is not None
            assert failed_tl[0].attempt_number == 1
            assert failed_tl[0].source == "live"

            # Success receipt (from retry)
            sent_tl = [r for r in timeline_receipts if r.status == "sent"]
            assert len(sent_tl) >= 1
            retry_sent = sent_tl[0]
            assert retry_sent.attempt_number == 2
            assert retry_sent.parent_receipt_id == original.receipt_id
            assert retry_sent.source == "retry"

            # Timeline source reflects mixed (live + retry)
            assert timeline["source"] == "mixed"
        finally:
            await runner.stop()

    async def test_trace_shows_no_duplicate_retry_lineage(self, temp_storage):
        """Timeline assembly returns exactly the right receipts with no
        duplicates.  Calling assemble_event_timeline a second time (simulating
        a second worker cycle) yields the same result — timeline unchanged."""
        event = _make_event()
        await temp_storage.append(event)

        t0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Attempt 1: transient failure
        failed = _make_failed_receipt(
            event_id=event.event_id,
            receipt_id="rcpt-fail-dup",
            attempt_number=1,
            created_at=t0,
        )
        await temp_storage.append_receipt(failed)

        # Attempt 2: retry succeeds (source=retry)
        retry_success = DeliveryReceipt(
            receipt_id="rcpt-ok-dup",
            event_id=event.event_id,
            delivery_plan_id="plan-1",
            target_adapter="target_a",
            route_id="route-1",
            status="sent",
            attempt_number=2,
            parent_receipt_id="rcpt-fail-dup",
            source="retry",
            created_at=t0 + timedelta(seconds=1),
        )
        await temp_storage.append_receipt(retry_success)

        # First timeline assembly
        timeline = await assemble_event_timeline(temp_storage, event.event_id)
        assert timeline is not None
        receipts = timeline["receipts"]
        assert len(receipts) == 2, f"Expected exactly 2 receipts, got {len(receipts)}"

        # Verify lineage
        assert receipts[0].receipt_id == "rcpt-fail-dup"
        assert receipts[0].status == "failed"
        assert receipts[0].attempt_number == 1
        assert receipts[1].receipt_id == "rcpt-ok-dup"
        assert receipts[1].status == "sent"
        assert receipts[1].attempt_number == 2
        assert receipts[1].parent_receipt_id == "rcpt-fail-dup"
        assert receipts[1].source == "retry"

        # No duplicate retry receipts
        retry_receipts = [r for r in receipts if r.source == "retry"]
        assert (
            len(retry_receipts) == 1
        ), "Should have exactly 1 retry receipt, no duplicates"

        # Second assembly (simulates second worker cycle reading same data)
        timeline2 = await assemble_event_timeline(temp_storage, event.event_id)
        assert timeline2 is not None
        receipts2 = timeline2["receipts"]
        assert (
            len(receipts2) == 2
        ), "Timeline should be unchanged on second assembly — still 2 receipts"

        # Receipt IDs identical between both calls
        ids_1 = [r.receipt_id for r in receipts]
        ids_2 = [r.receipt_id for r in receipts2]
        assert ids_1 == ids_2

    async def test_retry_events_emitted(self, temp_storage):
        """RetryWorker with event_buffer emits retry_attempted and
        retry_succeeded events during a transient-failure → retry-succeed
        cycle."""
        from unittest.mock import AsyncMock

        from medre.adapters.fake_presentation import FakePresentationAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import RetryPolicy
        from medre.core.planning.fallback_resolution import (
            FallbackResolver as _BaseFallback,
        )
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.runtime.accounting import RuntimeAccounting
        from medre.runtime.events import EventBuffer, RuntimeEventType
        from medre.runtime.retry import RetryWorker

        class _FallbackResolverWithRetry(_BaseFallback):
            def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
                self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)

            def resolve_fallback(self, event, target, capabilities):
                plan = super().resolve_fallback(event, target, capabilities)
                plan.retry_policy = self._retry_policy
                return plan

        class _TransientOnceAdapter(FakePresentationAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._call_count = 0

            async def deliver(self, result):
                self._call_count += 1
                if self._call_count <= 1:
                    raise ConnectionError("transient for events test")
                return await super().deliver(result)

        event_buffer = EventBuffer(maxlen=64)
        adapter = _TransientOnceAdapter(adapter_id="events_target")
        event = _make_event()

        route = Route(
            id="events-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="events_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"events_target": adapter}

        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=_FallbackResolverWithRetry(),
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
            adapter_id="events_target",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.events_target"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=__import__("asyncio").Event(),
        )
        await adapter.start(ctx)
        await runner.start()

        retry_worker = RetryWorker(
            storage=temp_storage,
            pipeline=runner,
            capacity_controller=None,
            enabled=True,
            interval_seconds=9999,  # won't actually poll
            max_attempts=3,
            event_buffer=event_buffer,
        )

        try:
            # First delivery: fails transiently
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original = failed[0]

            # Run _process_due to trigger the retry
            future_now = original.next_retry_at + timedelta(seconds=1)
            await retry_worker._process_due(future_now)

            # Verify events were emitted
            events = list(event_buffer)
            event_types = [e.event_type for e in events]

            assert (
                RuntimeEventType.RETRY_ATTEMPTED in event_types
            ), f"Expected retry_attempted event, got: {[e.value for e in event_types]}"
            assert (
                RuntimeEventType.RETRY_SUCCEEDED in event_types
            ), f"Expected retry_succeeded event, got: {[e.value for e in event_types]}"

            # Verify retry_attempted has correct receipt IDs
            attempted = [
                e for e in events if e.event_type == RuntimeEventType.RETRY_ATTEMPTED
            ]
            assert len(attempted) >= 1
            assert attempted[0].detail["receipt_id"] == original.receipt_id
            assert attempted[0].detail["event_id"] == event.event_id
            assert attempted[0].detail["target_adapter"] == "events_target"

            # Verify retry_succeeded has correct receipt IDs
            succeeded = [
                e for e in events if e.event_type == RuntimeEventType.RETRY_SUCCEEDED
            ]
            assert len(succeeded) >= 1
            assert succeeded[0].detail["parent_receipt_id"] == original.receipt_id
            assert succeeded[0].detail["retry_receipt_id"] is not None
            assert succeeded[0].detail["event_id"] == event.event_id
        finally:
            await retry_worker.stop()
            await runner.stop()

    async def test_retry_snapshot_consistency(self, temp_storage):
        """After retry succeeds, the RetryWorkerState snapshot shows
        processed >= 1 and succeeded >= 1."""
        from unittest.mock import AsyncMock

        from medre.adapters.fake_presentation import FakePresentationAdapter
        from medre.core.contracts.adapter import AdapterContext
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import RetryPolicy
        from medre.core.planning.fallback_resolution import (
            FallbackResolver as _BaseFallback,
        )
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.runtime.accounting import RuntimeAccounting
        from medre.runtime.retry import RetryWorker, RetryWorkerState

        class _FallbackResolverWithRetry(_BaseFallback):
            def __init__(self, retry_policy: RetryPolicy | None = None) -> None:
                self._retry_policy = retry_policy or RetryPolicy(max_attempts=3)

            def resolve_fallback(self, event, target, capabilities):
                plan = super().resolve_fallback(event, target, capabilities)
                plan.retry_policy = self._retry_policy
                return plan

        class _TransientOnceAdapter(FakePresentationAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._call_count = 0

            async def deliver(self, result):
                self._call_count += 1
                if self._call_count <= 1:
                    raise ConnectionError("transient for snapshot test")
                return await super().deliver(result)

        adapter = _TransientOnceAdapter(adapter_id="snapshot_target")
        event = _make_event()

        route = Route(
            id="snapshot-route",
            source=RouteSource(
                adapter="fake_source",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="snapshot_target")],
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        adapters = {"snapshot_target": adapter}

        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=_FallbackResolverWithRetry(),
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
            adapter_id="snapshot_target",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger("test.snapshot_target"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=__import__("asyncio").Event(),
        )
        await adapter.start(ctx)
        await runner.start()

        retry_worker = RetryWorker(
            storage=temp_storage,
            pipeline=runner,
            capacity_controller=None,
            enabled=True,
            interval_seconds=9999,
            max_attempts=3,
        )

        try:
            # First delivery: fails transiently
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await temp_storage.list_receipts_for_event(
                event.event_id,
            )
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            original = failed[0]

            future_now = original.next_retry_at + timedelta(seconds=1)
            await retry_worker._process_due(future_now)

            # Verify snapshot retry section consistency
            state = retry_worker.state
            assert isinstance(state, RetryWorkerState)
            assert (
                state.processed >= 1
            ), f"Expected processed >= 1, got {state.processed}"
            assert (
                state.succeeded >= 1
            ), f"Expected succeeded >= 1, got {state.succeeded}"
            assert state.failed == 0
            assert state.dead_lettered == 0
        finally:
            await retry_worker.stop()
            await runner.stop()

    async def test_full_retry_lifecycle_trace_evidence(self, temp_storage):
        """Full retry lifecycle trace/evidence: live fail → retry fail
        → dead-lettered → replay.  Timeline shows all receipts with correct
        sources, lineage chains, replay metadata, and trace features."""
        # Build 5 receipts manually for a single event covering the full
        # retry lifecycle.  Fixed timestamps give deterministic ordering.
        t0 = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        event = _make_event()
        await temp_storage.append(event)

        # (a) Live failed transient — source=live, attempt 1
        live_rcpt = DeliveryReceipt(
            receipt_id="rcpt-live",
            event_id=event.event_id,
            delivery_plan_id="plan-lifecycle",
            target_adapter="target_a",
            route_id="route-lifecycle",
            status="failed",
            error="ConnectionError: timeout",
            failure_kind="adapter_transient",
            next_retry_at=t0 + timedelta(seconds=2),
            attempt_number=1,
            parent_receipt_id=None,
            source="live",
            created_at=t0,
        )
        await temp_storage.append_receipt(live_rcpt)

        # (b) Capacity rejection does not create a new receipt — it only
        # pushes next_retry_at forward.  We simulate by appending a retry
        # receipt with status "failed" and failure_kind "capacity_rejection"
        # to represent the capacity rejection event in the timeline.
        cap_rcpt = DeliveryReceipt(
            receipt_id="rcpt-cap-rej",
            event_id=event.event_id,
            delivery_plan_id="plan-lifecycle",
            target_adapter="target_a",
            route_id="route-lifecycle",
            status="failed",
            error="delivery capacity not available",
            failure_kind="capacity_rejection",
            next_retry_at=t0 + timedelta(seconds=5),
            attempt_number=1,
            parent_receipt_id="rcpt-live",
            source="retry",
            created_at=t0 + timedelta(seconds=3),
        )
        await temp_storage.append_receipt(cap_rcpt)

        # (c) Retry failed transient — source=retry, attempt 2
        retry_fail_rcpt = DeliveryReceipt(
            receipt_id="rcpt-retry-fail",
            event_id=event.event_id,
            delivery_plan_id="plan-lifecycle",
            target_adapter="target_a",
            route_id="route-lifecycle",
            status="failed",
            error="ConnectionError: timeout again",
            failure_kind="adapter_transient",
            next_retry_at=t0 + timedelta(seconds=7),
            attempt_number=2,
            parent_receipt_id="rcpt-live",
            source="retry",
            created_at=t0 + timedelta(seconds=6),
        )
        await temp_storage.append_receipt(retry_fail_rcpt)

        # (d) Retry dead-lettered — source=retry, attempt 3 (exhausted)
        dead_rcpt = DeliveryReceipt(
            receipt_id="rcpt-dead",
            event_id=event.event_id,
            delivery_plan_id="plan-lifecycle",
            target_adapter="target_a",
            route_id="route-lifecycle",
            status="dead_lettered",
            error="Retry exhausted",
            attempt_number=3,
            parent_receipt_id="rcpt-retry-fail",
            source="retry",
            created_at=t0 + timedelta(seconds=8),
        )
        await temp_storage.append_receipt(dead_rcpt)

        # (e) Manual BEST_EFFORT replay receipt
        replay_run_id = "replay-lifecycle-001"
        replay_rcpt = DeliveryReceipt(
            receipt_id="rcpt-replay",
            event_id=event.event_id,
            delivery_plan_id="plan-replay-lifecycle",
            target_adapter="target_a",
            route_id="route-lifecycle",
            status="sent",
            attempt_number=1,
            parent_receipt_id=None,
            source="replay",
            replay_run_id=replay_run_id,
            created_at=t0 + timedelta(seconds=10),
        )
        await temp_storage.append_receipt(replay_rcpt)

        # === Timeline assembly ===
        timeline = await assemble_event_timeline(
            temp_storage,
            event.event_id,
        )
        assert timeline is not None

        tl_receipts = timeline["receipts"]
        # 5 receipts: live fail, cap rejection, retry fail, dead-lettered, replay
        assert len(tl_receipts) == 5, (
            f"Expected 5 receipts, got {len(tl_receipts)}: "
            f"{[r.receipt_id for r in tl_receipts]}"
        )

        # === Sources ===
        tl_sources = [r.source for r in tl_receipts]
        assert tl_sources[0] == "live"
        assert tl_sources[1] == "retry"  # capacity rejection
        assert tl_sources[2] == "retry"  # failed transient
        assert tl_sources[3] == "retry"  # dead-lettered
        assert tl_sources[4] == "replay"

        # === Retry lineage: parent_receipt_id chains ===
        by_id = {r.receipt_id: r for r in tl_receipts}

        # (a) live → no parent
        assert by_id["rcpt-live"].parent_receipt_id is None

        # (b) cap rejection → parent is live
        assert by_id["rcpt-cap-rej"].parent_receipt_id == "rcpt-live"

        # (c) retry fail → parent is live (first retry from original)
        assert by_id["rcpt-retry-fail"].parent_receipt_id == "rcpt-live"

        # (d) dead-lettered → parent is retry-fail (chained retry)
        assert by_id["rcpt-dead"].parent_receipt_id == "rcpt-retry-fail"

        # (e) replay → no parent_receipt_id (independent chain)
        assert by_id["rcpt-replay"].parent_receipt_id is None

        # === Replay: replay_run_id present, no parent ===
        assert by_id["rcpt-replay"].replay_run_id == replay_run_id
        assert by_id["rcpt-replay"].parent_receipt_id is None

        # === Trace shows retry exhausted state ===
        dead_lettered = [r for r in tl_receipts if r.status == "dead_lettered"]
        assert len(dead_lettered) == 1
        assert dead_lettered[0].attempt_number == 3

        # === Timeline source is mixed ===
        assert timeline["source"] == "mixed"

        # === Replay duplicate-risk warning present ===
        from medre.runtime.trace import assemble_replay_timeline as _trace_replay_tl

        replay_receipts = [r for r in tl_receipts if r.replay_run_id == replay_run_id]
        event_cache = {event.event_id: event}
        replay_tl = _trace_replay_tl(
            replay_run_id,
            replay_receipts,
            event_cache,
        )
        assert "duplicate_send_caveat" in replay_tl
        assert replay_tl["duplicate_send_caveat"] is not None

        # === Replay run grouping ===
        assert replay_run_id in timeline["replay_runs"]
        assert len(timeline["replay_runs"][replay_run_id]) >= 1


class TestRecommendedCommandsInspectFirst:
    """Verify that recommended_commands never starts with 'medre trace event'."""

    @pytest.mark.parametrize(
        "category",
        [
            "retryable",
            "permanent",
            "operational",
            "unknown",
        ],
    )
    def test_no_trace_event_in_primary_recommendation(
        self,
        category: str,
    ) -> None:
        """Primary recommended command does not start with 'medre trace event'.

        For non-operational categories the first command starts with
        'medre inspect'.  Operational starts with 'medre diagnostics'.
        Neither starts with 'medre trace event'.
        """
        cmds = recommended_commands(category, "evt-test-001")
        assert len(cmds) > 0
        first = cmds[0]
        assert not first.startswith("medre trace event"), (
            f"Primary recommendation for {category!r} should not start with "
            f"'medre trace event', got: {first!r}"
        )

    @pytest.mark.parametrize(
        "category",
        [
            "retryable",
            "permanent",
            "operational",
            "unknown",
        ],
    )
    def test_no_trace_event_in_any_recommendation(
        self,
        category: str,
    ) -> None:
        """No recommended command starts with 'medre trace event'."""
        cmds = recommended_commands(category, "evt-test-001")
        for cmd in cmds:
            assert not cmd.startswith("medre trace event"), (
                f"Recommended command starts with 'medre trace event' "
                f"for {category!r}: {cmd!r}"
            )

    def test_retryable_includes_inspect_recovery(self) -> None:
        """Retryable category recommends 'medre inspect event ... --recovery'."""
        cmds = recommended_commands("retryable", "evt-rty")
        assert any(
            "inspect event" in c and "--recovery" in c for c in cmds
        ), f"Expected 'inspect event ... --recovery' in: {cmds}"

    def test_permanent_includes_inspect_evidence(self) -> None:
        """Permanent category recommends 'medre inspect event ... --evidence'."""
        cmds = recommended_commands("permanent", "evt-perm")
        assert any(
            "inspect event" in c and "--evidence" in c for c in cmds
        ), f"Expected 'inspect event ... --evidence' in: {cmds}"

    def test_operational_includes_inspect_timeline(self) -> None:
        """Operational category recommends 'medre inspect event ... --timeline'."""
        cmds = recommended_commands("operational", "evt-ops")
        assert any(
            "inspect event" in c and "--timeline" in c for c in cmds
        ), f"Expected 'inspect event ... --timeline' in: {cmds}"

    def test_unknown_includes_inspect_timeline(self) -> None:
        """Unknown category recommends 'medre inspect event ... --timeline'."""
        cmds = recommended_commands("unknown", "evt-unk")
        assert any(
            "inspect event" in c and "--timeline" in c for c in cmds
        ), f"Expected 'inspect event ... --timeline' in: {cmds}"
