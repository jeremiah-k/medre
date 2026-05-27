"""Track 5: Replay/routing durability tests.

Covers replay cancellation, degraded pipeline behaviour, shutdown
semantics, repeated-cycle determinism, observability consistency,
route filtering under pressure, and capacity-pressure resilience.

All tests use fake adapters / no live transports.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from medre.config.model import RuntimeLimits
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.planning import FallbackResolver
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    ReplaySummary,
    _build_summary,
    collect_replay_state,
    collect_replay_summary,
)
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    source_adapter: str = "adapter_a",
    event_kind: str = "message.created",
    source_channel_id: str | None = "ch-0",
    *,
    event_id: str = "evt-1",
    metadata: EventMetadata | None = None,
) -> CanonicalEvent:
    """Create a minimal CanonicalEvent for durability tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=metadata or EventMetadata(),
    )


class _StubPipeline:
    """Minimal pipeline collaborator satisfying _PipelineProtocol for tests."""

    def __init__(
        self,
        router: Router | None = None,
        rendering_pipeline: RenderingPipeline | None = None,
    ) -> None:
        self._router = router
        self._rendering_pipeline = rendering_pipeline
        self._fallback_resolver = FallbackResolver()

    async def transform_event(self, event: CanonicalEvent) -> CanonicalEvent:
        return event

    async def render_event(self, event: CanonicalEvent) -> Any:
        if self._rendering_pipeline is not None:
            return await self._rendering_pipeline.render(event, "test_adapter")
        return None

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> list[tuple[Any, list[Any]]]:
        if self._router is None:
            return []
        results: list[tuple[Any, list[Any]]] = []
        for route in self._router.match(event):
            targets = self._router.resolve_targets(event, route)
            results.append((route, targets))
        return results

    async def plan_delivery(
        self,
        event: CanonicalEvent,
        routes: list[tuple[Any, list[Any]]],
    ) -> list[Any]:
        plans: list[Any] = []
        for _route, targets in routes:
            for target in targets:
                plan = self._fallback_resolver.resolve_fallback(
                    event, target, AdapterCapabilities()
                )
                plans.append(plan)
        return plans

    async def deliver(self, event: CanonicalEvent, plans: list[Any]) -> list[Any]:
        return plans


def _make_engine(
    storage: SQLiteStorage,
    pipeline: Any | None = None,
    capacity_controller: CapacityController | None = None,
    accounting: RuntimeAccounting | None = None,
) -> ReplayEngine:
    """Create a ReplayEngine wired with optional capacity controller and accounting."""
    engine = ReplayEngine(
        storage=cast(StorageBackend, storage),
        pipeline=pipeline,
        capacity_controller=capacity_controller,
        accounting=accounting,
    )
    if capacity_controller is not None:
        engine.set_capacity_controller(capacity_controller)
    return engine


def _make_router(
    source: str = "adapter_a",
    dests: tuple[str, ...] = ("adapter_b",),
    route_id: str = "route-1",
    event_kinds: tuple[str, ...] = (),
) -> Router:
    """Create a Router with a single route."""
    route = Route(
        id=route_id,
        source=RouteSource(adapter=source, event_kinds=event_kinds, channel=None),
        targets=[RouteTarget(adapter=d) for d in dests],
    )
    return Router(routes=[route])


def _make_capacity(limits: RuntimeLimits | None = None) -> CapacityController:
    """Create a CapacityController with given or minimal limits."""
    if limits is None:
        limits = RuntimeLimits(
            max_inflight_deliveries=2,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=0.1,
        )
    return CapacityController(limits)


# ===================================================================
# 1. Replay cancellation via capacity controller
# ===================================================================


class TestReplayCancellation:
    """Replay BEST_EFFORT respects capacity controller cancellation."""

    async def test_stop_accepting_before_replay(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When CC is stopped before replay, BEST_EFFORT deliver gets capacity error + accounting."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        cc = _make_capacity()
        cc.stop_accepting()
        accounting = RuntimeAccounting()

        engine = _make_engine(
            temp_storage,
            pipeline=pipeline,
            capacity_controller=cc,
            accounting=accounting,
        )
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]
        deliver_result = results[4]  # store, route, plan, render, deliver
        assert deliver_result.stage == "deliver"
        assert deliver_result.status == "error"
        assert "replay_rejected_shutdown" in (deliver_result.error or "")

        # Accounting: capacity_rejections incremented.
        assert accounting.counters().capacity_rejections == 1

    async def test_strict_mode_ignores_capacity(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """STRICT mode does not involve the capacity controller at all."""
        event = _make_event()
        await temp_storage.append(event)

        cc = _make_capacity()
        cc.stop_accepting()

        engine = _make_engine(temp_storage, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "passed"

    async def test_dry_run_ignores_capacity(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """DRY_RUN suppresses delivery, so capacity controller is not consulted."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        cc = _make_capacity()
        cc.stop_accepting()

        engine = _make_engine(temp_storage, pipeline=pipeline, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        results = [r async for r in engine.replay(request)]
        deliver_result = results[4]
        assert deliver_result.status == "skipped"
        assert "dry_run" in (deliver_result.error or "")

    async def test_capacity_rejection_snapshot(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """CapacityController snapshot reflects replay rejections."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        cc = _make_capacity()
        cc.stop_accepting()

        engine = _make_engine(temp_storage, pipeline=pipeline, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)
        _ = [r async for r in engine.replay(request)]

        snap = cc.snapshot()
        assert snap["replay_rejections"] >= 1
        assert snap["accepting_work"] is False


# ===================================================================
# 2. Replay degraded pipeline
# ===================================================================


class TestReplayDegradedPipeline:
    """Replay handles degraded pipeline conditions gracefully."""

    async def test_route_error_per_event_isolation(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """One event's route error doesn't prevent another event from succeeding."""
        evt_a = _make_event(source_adapter="a", event_id="evt-a")
        evt_b = _make_event(source_adapter="b", event_id="evt-b")
        await temp_storage.append(evt_a)
        await temp_storage.append(evt_b)

        call_count = 0

        async def _route_side_effect(event: CanonicalEvent) -> Any:
            nonlocal call_count
            call_count += 1
            if event.event_id == "evt-a":
                raise RuntimeError("route degraded for evt-a")
            return [("route", [RouteTarget(adapter="dst")])]

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(side_effect=lambda e: e)
        pipeline.route_event = AsyncMock(side_effect=_route_side_effect)
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # evt-a: store + route(error) + plan(skipped) = 3
        # evt-b: store + route(passed) + plan(passed) = 3
        assert len(results) == 6

        # evt-a route error
        route_a = results[1]
        assert route_a.event_id == "evt-a"
        assert route_a.status == "error"
        assert "route degraded" in (route_a.error or "")

        # evt-b route success
        route_b = results[4]
        assert route_b.event_id == "evt-b"
        assert route_b.status == "passed"

    async def test_deliver_error_best_effort_captures(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT capture adapter delivery error per event."""
        evt_a = _make_event(source_adapter="src", event_id="evt-a")
        evt_b = _make_event(source_adapter="src", event_id="evt-b")
        await temp_storage.append(evt_a)
        await temp_storage.append(evt_b)

        call_count = 0

        async def _deliver_side_effect(event: CanonicalEvent, plans: list) -> Any:
            nonlocal call_count
            call_count += 1
            if event.event_id == "evt-a":
                raise RuntimeError("adapter exploded")
            return ["receipt"]

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(side_effect=lambda e: e)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(side_effect=_deliver_side_effect)

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]

        # Find deliver results
        deliver_a = results[4]
        deliver_b = results[9]
        assert deliver_a.event_id == "evt-a"
        assert deliver_a.status == "error"
        assert "adapter exploded" in (deliver_a.error or "")

        assert deliver_b.event_id == "evt-b"
        assert deliver_b.status == "passed"

    async def test_render_error_in_re_render_mode(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RE_RENDER mode captures render errors gracefully."""
        event = _make_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(
            side_effect=RuntimeError("renderer crashed"),
        )

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2  # store + render
        render_result = results[1]
        assert render_result.status == "error"
        assert "renderer crashed" in (render_result.error or "")

    async def test_multiple_failures_still_produces_consistent_summary(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Multiple failures across events produce a consistent summary."""
        evt_a = _make_event(source_adapter="a", event_id="evt-a")
        evt_b = _make_event(source_adapter="b", event_id="evt-b")
        await temp_storage.append(evt_a)
        await temp_storage.append(evt_b)

        async def _route_side_effect(event: CanonicalEvent) -> Any:
            if event.event_id == "evt-a":
                raise RuntimeError("route failure")
            return []  # no routes match evt-b

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(side_effect=lambda e: e)
        pipeline.route_event = AsyncMock(side_effect=_route_side_effect)
        pipeline.plan_delivery = AsyncMock(return_value=[])

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        summary = await collect_replay_summary(
            engine.replay(request),
            mode=ReplayMode.RE_ROUTE,
        )
        assert summary.events_replayed == 6  # 2 events × 3 stages
        assert summary.failure_count >= 2  # at least 2 failures
        assert len(summary.errors) >= 1


# ===================================================================
# 3. Replay shutdown semantics
# ===================================================================


class TestReplayShutdown:
    """Replay handles shutdown / stop-accepting gracefully."""

    async def test_shutdown_mid_replay_best_effort(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Stopping CC mid-replay causes subsequent events to get capacity errors."""
        for i in range(4):
            await temp_storage.append(
                _make_event(source_adapter="src", event_id=f"evt-{i}"),
            )

        call_count = 0

        async def _deliver_side_effect(event: CanonicalEvent, plans: list) -> Any:
            nonlocal call_count
            call_count += 1
            return ["receipt"]

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(side_effect=lambda e: e)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(side_effect=_deliver_side_effect)

        cc = _make_capacity()
        engine = _make_engine(temp_storage, pipeline=pipeline, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = []
        async for result in engine.replay(request):
            results.append(result)
            # After the second event's deliver, stop accepting
            if result.stage == "deliver" and result.event_id == "evt-1":
                cc.stop_accepting()

        # Some events after stop_accepting should have capacity errors
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) == 4
        # First 2 should have succeeded (or been in-flight before stop)
        # Remaining should have capacity errors
        rejected_errors = [
            r
            for r in deliver_results
            if r.status == "error"
            and (
                "replay_rejected_shutdown" in (r.error or "")
                or "replay_capacity_exceeded" in (r.error or "")
            )
        ]
        assert len(rejected_errors) >= 1

    async def test_shutdown_does_not_affect_read_only_modes(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RE_ROUTE and STRICT modes are unaffected by CC stop_accepting."""
        for i in range(3):
            await temp_storage.append(
                _make_event(source_adapter="a", event_id=f"evt-{i}"),
            )

        cc = _make_capacity()
        cc.stop_accepting()

        router = _make_router(source="a", dests=("b",))
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 9  # 3 events × 3 stages
        # All should pass or fail for routing reasons, not capacity
        for r in results:
            if r.status == "error":
                assert "replay_capacity" not in (r.error or "")


# ===================================================================
# 4. Repeated-cycle determinism
# ===================================================================


class TestReplayRepeatedCycles:
    """Running the same replay multiple times produces identical results."""

    async def test_five_identical_replay_cycles(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Five RE_ROUTE cycles produce identical results and attribution."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r-cycle")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        all_runs: list[list[ReplayResult]] = []
        for _ in range(5):
            results = [r async for r in engine.replay(request)]
            all_runs.append(results)

        for i in range(1, 5):
            assert len(all_runs[0]) == len(all_runs[i])
            for r1, r2 in zip(all_runs[0], all_runs[i], strict=False):
                assert r1.event_id == r2.event_id
                assert r1.stage == r2.stage
                assert r1.status == r2.status
                assert r1.route_attribution == r2.route_attribution

    async def test_summary_deterministic_across_five_cycles(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplaySummary is identical across five replay cycles."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r-sum")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)

        summaries: list[ReplaySummary] = []
        for _ in range(5):
            summary = await collect_replay_summary(
                engine.replay(ReplayRequest(mode=ReplayMode.RE_ROUTE)),
                mode=ReplayMode.RE_ROUTE,
            )
            summaries.append(summary)

        for i in range(1, 5):
            assert summaries[i].events_replayed == summaries[0].events_replayed
            assert summaries[i].failure_count == summaries[0].failure_count
            assert summaries[i].skipped_count == summaries[0].skipped_count
            assert summaries[i].by_status == summaries[0].by_status
            assert summaries[i].by_route == summaries[0].by_route
            assert (
                summaries[i].route_resolution_count
                == summaries[0].route_resolution_count
            )

    async def test_loop_warnings_deterministic_across_cycles(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Loop warnings are identical across repeated cycles even with loop routes."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("a",), route_id="loop-r")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        warnings_per_run: list[tuple[str, ...]] = []
        for _ in range(5):
            results = [r async for r in engine.replay(request)]
            route_result = results[1]
            assert route_result.route_attribution is not None
            warnings_per_run.append(route_result.route_attribution.loop_warnings)

        for w in warnings_per_run[1:]:
            assert w == warnings_per_run[0]


# ===================================================================
# 5. Observability consistency
# ===================================================================


class TestObservabilityConsistency:
    """RouteStats, ReplaySummary, and route_trace are consistent."""

    def test_route_stats_snapshot_after_failures(self) -> None:
        """RouteStats correctly records delivered + failed + skipped."""
        stats = RouteStats()
        stats.record_delivered("r1")
        stats.record_delivered("r1")
        stats.record_failed("r1", "timeout")
        stats.record_skipped("r2")
        stats.record_loop_prevented("r2")
        stats.record_failed("r2", "connection refused")

        snap = stats.snapshot()
        assert snap["r1"]["delivered"] == 2
        assert snap["r1"]["failed"] == 1
        assert "timeout" in snap["r1"]["last_error"]
        assert snap["r2"]["skipped"] == 1
        assert snap["r2"]["loop_prevented"] == 1
        assert snap["r2"]["failed"] == 1
        assert "connection refused" in snap["r2"]["last_error"]

    def test_route_stats_snapshot_sorted_deterministic(self) -> None:
        """RouteStats snapshot keys are sorted alphabetically."""
        stats = RouteStats()
        stats.record_delivered("zebra")
        stats.record_delivered("alpha")
        stats.record_delivered("mid")

        snap = stats.snapshot()
        assert list(snap.keys()) == ["alpha", "mid", "zebra"]

    def test_route_stats_snapshot_error_sanitized(self) -> None:
        """RouteStats sanitizes error messages (tokens, long strings)."""
        stats = RouteStats()
        stats.record_failed(
            "r1",
            "token=syt_abc123ABCDEFG api_key=sk-abc123def456ghi789jkl password=secret123",
        )
        snap = stats.snapshot()
        error = snap["r1"]["last_error"]
        assert "syt_" not in error
        assert "sk-" not in error
        assert "password=" not in error
        assert "[REDACTED]" in error

    def test_replay_summary_json_serializable(self) -> None:
        """ReplaySummary.to_dict() is JSON-serializable with sort_keys=True."""
        summary = _build_summary(
            [
                ReplayResult(
                    event_id="e1",
                    stage="store",
                    status="passed",
                ),
                ReplayResult(
                    event_id="e1",
                    stage="route",
                    status="passed",
                    output=[("route", ["target"])],
                    route_attribution=ReplayRouteAttribution(
                        route_ids=("r1",),
                        source_adapter="a",
                        target_adapters=("b",),
                        replay_mode="re_route",
                        run_id="run-42",
                    ),
                ),
                ReplayResult(
                    event_id="e1",
                    stage="plan",
                    status="passed",
                ),
            ],
            events_scanned=1,
            elapsed_ms=42.5,
            mode=ReplayMode.RE_ROUTE,
            run_id="run-42",
        )
        d = summary.to_dict()
        # Must not raise
        serialized = json.dumps(d, sort_keys=True)
        assert isinstance(serialized, str)
        assert "run-42" in serialized

    async def test_route_trace_boundedness_across_replays(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """route_trace does not grow unboundedly across repeated replays.

        Each replay re-routes the event, and the enriched event may accumulate
        route_trace entries.  After N replays, the route_trace length should
        remain bounded (not N * matched_routes).
        """
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r-trace")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        trace_lengths: list[int] = []
        for _ in range(10):
            results = [r async for r in engine.replay(request)]
            results[1]
            # The route output contains the enriched event (via real pipeline)
            # or the stub returns (route, targets).  The stored event is not
            # mutated, so route_trace should stay bounded.
            stored = await temp_storage.get(event.event_id)
            assert stored is not None
            if stored.metadata.routing is not None:
                trace_lengths.append(len(stored.metadata.routing.route_trace))
            else:
                trace_lengths.append(0)

        # Since replay is read-only and doesn't mutate storage, the stored
        # event's route_trace should be 0 (or the original value) across
        # all replays — it should NOT grow linearly.
        assert all(
            t == trace_lengths[0] for t in trace_lengths
        ), f"route_trace length changed across replays: {trace_lengths}"
        assert trace_lengths[0] <= 2  # original event has no routing

    async def test_replay_state_matches_summary(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """ReplayState counters are consistent with ReplaySummary counts."""
        event = _make_event(source_adapter="a")
        await temp_storage.append(event)

        router = _make_router(source="a", dests=("b",), route_id="r-cons")
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        # Collect both state and summary from the same replay
        state = await collect_replay_state(engine.replay(request))
        summary = await collect_replay_summary(
            engine.replay(request),
            mode=ReplayMode.RE_ROUTE,
        )

        assert state.events_processed == summary.events_replayed
        assert state.events_passed == summary.by_status.get("passed", 0)
        assert state.events_skipped == summary.by_status.get("skipped", 0)
        assert state.events_failed == (
            summary.by_status.get("failed", 0) + summary.by_status.get("error", 0)
        )

    def test_summary_error_truncation(self) -> None:
        """_build_summary truncates error messages to _MAX_ERROR_LENGTH."""
        from medre.core.storage.replay import _MAX_ERROR_LENGTH

        long_error = "x" * 600
        results = [
            ReplayResult(
                event_id="e1",
                stage="store",
                status="error",
                error=long_error,
            ),
        ]
        summary = _build_summary(results)
        assert len(summary.errors) == 1
        assert len(summary.errors[0]) == _MAX_ERROR_LENGTH

    def test_summary_error_count_cap(self) -> None:
        """_build_summary caps the number of retained errors."""
        from medre.core.storage.replay import _MAX_SUMMARY_ERRORS

        results = [
            ReplayResult(
                event_id=f"e{i}",
                stage="store",
                status="error",
                error=f"error {i}",
            )
            for i in range(100)
        ]
        summary = _build_summary(results)
        assert len(summary.errors) == _MAX_SUMMARY_ERRORS
        # First errors are retained
        assert summary.errors[0] == "error 0"
        assert summary.errors[-1] == f"error {_MAX_SUMMARY_ERRORS - 1}"


# ===================================================================
# 6. Route filtering under pressure
# ===================================================================


class TestRouteFilteringUnderPressure:
    """Multiple events with mixed route configurations are handled correctly."""

    async def test_many_events_mixed_loop_filtering(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Multiple events with varying loop configurations filter correctly."""
        # Event from a: should loop on a→a, pass on a→b
        await temp_storage.append(_make_event(source_adapter="a", event_id="ea"))
        # Event from b: should loop on b→a→b chain... actually b→b loops
        await temp_storage.append(_make_event(source_adapter="b", event_id="eb"))
        # Event from c: no loop
        await temp_storage.append(_make_event(source_adapter="c", event_id="ec"))

        r_loop_a = Route(
            id="loop-a",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="a")],  # loop
        )
        r_a_to_b = Route(
            id="a-to-b",
            source=RouteSource(adapter="a", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],  # valid
        )
        r_loop_b = Route(
            id="loop-b",
            source=RouteSource(adapter="b", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="b")],  # loop
        )
        r_c_to_a = Route(
            id="c-to-a",
            source=RouteSource(adapter="c", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="a")],  # valid
        )

        router = Router(routes=[r_loop_a, r_a_to_b, r_loop_b, r_c_to_a])
        pipeline = _StubPipeline(router=router)
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 9  # 3 events × 3 stages

        # Event ea: loop-a filtered, a-to-b passes
        route_ea = results[1]
        assert route_ea.status == "passed"
        assert route_ea.route_attribution is not None
        assert "a-to-b" in route_ea.route_attribution.route_ids
        assert "loop-a" not in route_ea.route_attribution.route_ids

        # Event eb: loop-b filtered, no other routes for b
        route_eb = results[4]
        assert route_eb.status == "failed"

        # Event ec: c-to-a passes
        route_ec = results[7]
        assert route_ec.status == "passed"
        assert route_ec.route_attribution is not None
        assert "c-to-a" in route_ec.route_attribution.route_ids

    async def test_target_adapter_filtering_under_replay(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """target_adapters filter correctly excludes non-matching adapters."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        # Create a mock plan that has .target.adapter so the filter can inspect it
        class _MockPlan:
            def __init__(self, adapter: str) -> None:
                self.target = RouteTarget(adapter=adapter)
                self.plan_id = "plan-1"

        mock_plan = _MockPlan("dst-a")

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst-a")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=[mock_plan])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            target_adapters=["dst-other"],  # doesn't match dst-a
        )

        results = [r async for r in engine.replay(request)]
        deliver_result = results[4]
        assert deliver_result.status == "skipped"
        assert "target_adapters" in (deliver_result.error or "")


# ===================================================================
# 7. Capacity pressure
# ===================================================================


class TestReplayCapacityPressure:
    """Replay under capacity pressure behaves correctly."""

    async def test_single_replay_slot_contention(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Multiple BEST_EFFORT events contend for a single replay slot."""
        for i in range(3):
            await temp_storage.append(
                _make_event(source_adapter="src", event_id=f"evt-{i}"),
            )

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(side_effect=lambda e: e)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        # Single replay slot
        limits = RuntimeLimits(
            max_inflight_deliveries=1,
            max_inflight_replay_events=1,
            delivery_acquire_timeout_seconds=1.0,
        )
        cc = _make_capacity(limits)

        engine = _make_engine(temp_storage, pipeline=pipeline, capacity_controller=cc)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]

        # All events should have been replayed (sequential due to single slot)
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) == 3
        # All should have passed since they acquire/release sequentially
        for dr in deliver_results:
            assert dr.status == "passed"

        # Verify capacity controller is clean
        snap = cc.snapshot()
        assert snap["replay_current"] == 0
        assert snap["replay_rejections"] == 0

    async def test_capacity_snapshot_tracks_state(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """CapacityController snapshot accurately tracks state."""
        limits = RuntimeLimits(
            max_inflight_deliveries=5,
            max_inflight_replay_events=2,
            delivery_acquire_timeout_seconds=1.0,
        )
        cc = _make_capacity(limits)

        snap = cc.snapshot()
        assert snap["replay_limit"] == 2
        assert snap["delivery_limit"] == 5
        assert snap["replay_current"] == 0
        assert snap["delivery_current"] == 0
        assert snap["accepting_work"] is True

        cc.stop_accepting()
        snap = cc.snapshot()
        assert snap["accepting_work"] is False

    async def test_best_effort_without_capacity_controller(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """BEST_EFFORT works without a capacity controller (no capacity guard)."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(return_value=["receipt"])

        # No capacity controller
        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        results = [r async for r in engine.replay(request)]
        deliver_result = results[4]
        assert deliver_result.status == "passed"
        assert isinstance(deliver_result.output, dict)
        assert deliver_result.output["replay"] is True


# ===================================================================
# CancelledError propagation through replay
# ===================================================================


class TestCancelledErrorPropagation:
    """Verify that asyncio.CancelledError propagates through replay
    and is not swallowed by the BEST_EFFORT error handler.
    """

    async def test_cancelled_error_propagates_from_stage(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """CancelledError raised inside a stage propagates (not swallowed)."""
        event = _make_event()
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.route_event = AsyncMock(side_effect=asyncio.CancelledError())

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        with pytest.raises(asyncio.CancelledError):
            _ = [r async for r in engine.replay(request)]

    async def test_cancelled_error_propagates_from_deliver(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """CancelledError raised during delivery propagates (not swallowed)."""
        event = _make_event(source_adapter="src")
        await temp_storage.append(event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=event)
        pipeline.render_event = AsyncMock(return_value="rendered")
        pipeline.route_event = AsyncMock(
            return_value=[("route", [RouteTarget(adapter="dst")])],
        )
        pipeline.plan_delivery = AsyncMock(return_value=["plan"])
        pipeline.deliver = AsyncMock(side_effect=asyncio.CancelledError())

        engine = _make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        with pytest.raises(asyncio.CancelledError):
            _ = [r async for r in engine.replay(request)]
