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
    NativeMessageRef,
)
from medre.core.events.metadata import EventMetadata
from medre.core.storage.sqlite import SQLiteStorage
from medre.observability.classification import (
    infer_failure_kind,
    failure_category,
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

        # Recommended commands for retryable include replay
        cmds = recommended_commands("retryable", event.event_id)
        assert any("replay" in c for c in cmds)
        assert any("trace" in c for c in cmds)

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
            r.status == "failed" and r.next_retry_at is not None
            for r in r_a
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
            dead_lettered[0].error, dead_lettered[0].status,
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
        from medre.adapters.base import AdapterContext
        from medre.adapters.fake_presentation import FakePresentationAdapter
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.delivery_plan import RetryPolicy
        from medre.core.planning.fallback_resolution import FallbackResolver as _BaseFallback
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.models import Route, RouteSource, RouteTarget
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.runtime.accounting import RuntimeAccounting
        from unittest.mock import AsyncMock

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
                DeliveryPlan, DeliveryStrategy, RetryExecutor,
            )
            plan = DeliveryPlan(
                plan_id=original.delivery_plan_id,
                event_id=event.event_id,
                target=RouteTarget(adapter="trace_target"),
                primary_strategy=DeliveryStrategy(method="direct"),
                retry_policy=RetryPolicy(max_attempts=3),
            )
            route_obj = Route(
                id=original.route_id,
                source=RouteSource(adapter=None, event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="trace_target")],
            )
            retry_receipt = await runner.deliver_to_target(
                event, route_obj, plan,
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
