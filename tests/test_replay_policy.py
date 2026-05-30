"""BEST_EFFORT/DRY_RUN behavior, missing events, filters, limits.

Tests policy-level replay semantics: dry_run stage execution, storage
immutability guarantees, best_effort error tolerance, target_adapters
filtering, dead-letter retry, and non-mutation constraints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from medre.core.engine.replay import (
    ReplayMode,
    ReplayRequest,
)
from medre.core.events import CanonicalEvent, DeliveryReceipt
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Router
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import (
    StubPipeline,
    make_engine,
)


class TestReplayPolicy:
    """Replay mode policy: what each mode guarantees and forbids."""

    # ------------------------------------------------------------------
    # DRY_RUN mode
    # ------------------------------------------------------------------

    async def test_dry_run_executes_all_stages_except_delivery(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN runs store, route, plan, render but skips delivery."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.DRY_RUN,
        )

        results = [r async for r in engine.replay(request)]

        # store + route + plan + render + deliver (skipped)
        assert len(results) == 5
        stages = [r.stage for r in results]
        assert stages == ["store", "route", "plan", "render", "deliver"]

        # All stages pass except deliver which is skipped
        assert results[0].status == "passed"
        assert results[1].status == "passed"
        assert results[2].status == "passed"
        assert results[3].status == "passed"
        assert results[4].status == "skipped"
        assert "dry_run" in (results[4].error or "")

    async def test_dry_run_produces_no_storage_side_effects(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN mode does not change stored event count."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        _ = [r async for r in engine.replay(request)]

        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 1

    # ------------------------------------------------------------------
    # BEST_EFFORT tolerance
    # ------------------------------------------------------------------

    async def test_best_effort_tolerates_missing_adapter(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT mode returns error results instead of crashing."""
        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Adapter 'missing' not found"),
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
        )

        # Must not raise -- error is captured in results
        results = [r async for r in engine.replay(request)]

        assert len(results) == 5  # store, route, plan, render, deliver

        # The deliver stage should have error status
        deliver_result = results[4]
        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "error"
        assert "Adapter 'missing' not found" in (deliver_result.error or "")

    async def test_best_effort_adapter_failure_diagnostics(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT adapter failure records diagnostic events."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Adapter 'gone' not registered"),
        )

        diag = Diagnostician()
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 5
        assert results[4].status == "error"

        snap = diag.snapshot()
        assert len(snap["adapter_failures"]) > 0

    # ------------------------------------------------------------------
    # Non-BEST_EFFORT modes: no storage side effects
    # ------------------------------------------------------------------

    async def test_re_route_no_storage_mutation(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
    ) -> None:
        """RE_ROUTE never writes to storage."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(router=router_with_routes)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        _ = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        assert count_before == count_after == 1

    async def test_dry_run_no_storage_mutation(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN never writes to storage."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        _ = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        assert count_before == count_after == 1

    # ------------------------------------------------------------------
    # target_adapters filtering
    # ------------------------------------------------------------------

    async def test_target_adapters_filters_delivery(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """target_adapters excludes delivery to non-listed adapters."""
        await temp_storage.append(sample_event)

        # Create a plan-like object with a target.adapter attribute
        class _FakeTarget:
            def __init__(self, adapter: str) -> None:
                self.adapter = adapter

        class _FakePlan:
            def __init__(self, adapter: str) -> None:
                self.target = _FakeTarget(adapter)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=[_FakePlan("adapter_a")])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.BEST_EFFORT,
            target_adapters=["other_adapter"],
        )

        results = [r async for r in engine.replay(request)]
        # The deliver stage should be skipped because no plans match
        deliver_result = results[4]
        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "skipped"
        assert "target_adapters" in (deliver_result.error or "")

    # ------------------------------------------------------------------
    # Replay never mutates CanonicalEvent
    # ------------------------------------------------------------------

    async def test_replay_never_mutates_historical_event(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """All modes preserve the original event bytes and fields."""
        await temp_storage.append(sample_event)

        # Snapshot original
        orig_id = sample_event.event_id
        orig_kind = sample_event.event_kind
        orig_payload = dict(sample_event.payload)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)

        for mode in (
            ReplayMode.STRICT,
            ReplayMode.RE_RENDER,
            ReplayMode.RE_ROUTE,
            ReplayMode.DRY_RUN,
        ):
            request = ReplayRequest(mode=mode)
            _ = [r async for r in engine.replay(request)]

            # Original object unchanged
            assert sample_event.event_id == orig_id
            assert sample_event.event_kind == orig_kind
            assert dict(sample_event.payload) == orig_payload

            # Stored version unchanged
            stored = await temp_storage.get(sample_event.event_id)
            assert stored is not None
            assert stored.event_id == orig_id
            assert stored.event_kind == orig_kind
            assert dict(stored.payload) == orig_payload

    # ------------------------------------------------------------------
    # Dead-letter / retry replay semantics
    # ------------------------------------------------------------------

    async def test_dead_letter_retry_replay_via_best_effort(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT replay of a previously-failed event captures error.

        This tests the 'retry' semantics: replaying an event whose prior
        delivery failed.  In Phase 1, retry is BEST_EFFORT replay scoped
        to events with failed delivery receipts, not a separate mode.
        """
        # Store event and a failed receipt for it
        await temp_storage.append(sample_event)

        failed_receipt = DeliveryReceipt(
            sequence=0,
            receipt_id="rcpt-retry-001",
            event_id=sample_event.event_id,
            delivery_plan_id="plan-001",
            target_adapter="broken_adapter",
            status="failed",
            error="Connection refused",
        )
        await temp_storage.append_receipt(failed_receipt)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(return_value=[("route", ["target"])])
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(
            side_effect=RuntimeError("Still broken"),
        )

        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            correlation_ids=[sample_event.event_id],
            mode=ReplayMode.BEST_EFFORT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 5
        deliver_result = results[4]
        assert deliver_result.status == "error"
        assert "Still broken" in (deliver_result.error or "")
