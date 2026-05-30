"""Replay accounting: replay_processed, replay_rejected, capacity_rejections.

Focused tests for RuntimeAccounting replay counter wiring, including
processed/rejected increments, graceful degradation without accounting,
counter isolation, and observability snapshots.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from tests.helpers.replay import (
    StubPipeline,
    make_engine,
    make_second_event,
)


class TestReplayAccounting:
    """Focused tests for RuntimeAccounting replay counter wiring."""

    async def test_strict_replay_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Successful STRICT replay increments replay_processed."""
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_re_render_replay_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """Successful RE_RENDER replay increments replay_processed."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(rendering_pipeline=rendering_pipeline)
        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_missing_event_increments_rejected(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Missing event via correlation_ids increments replay_rejected."""
        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(
            correlation_ids=["nonexistent-id"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        c = acc.counters()
        assert c.replay_rejected == 1
        assert c.replay_processed == 0

    async def test_best_effort_crash_increments_rejected(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """BEST_EFFORT replay where _replay_event raises increments rejected.

        All individual stages catch their own exceptions, so triggering this
        path requires an error in the orchestration layer itself.  We patch
        _replay_event to raise, simulating an unrecoverable crash.
        """
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.BEST_EFFORT)

        import unittest.mock

        async def _fake_replay_event(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("unrecoverable crash")
            yield  # noqa: unreachable -- makes this an async generator

        with unittest.mock.patch.object(
            engine,
            "_replay_event",
            _fake_replay_event,
        ):
            results = [r async for r in engine.replay(request)]

        assert len(results) == 1
        assert results[0].status == "error"
        assert "unrecoverable crash" in (results[0].error or "")

        c = acc.counters()
        assert c.replay_rejected == 1
        assert c.replay_processed == 0

    async def test_multiple_events_accumulate_counters(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Two successful events + one missing -> processed=2, rejected=1."""
        second = make_second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, accounting=acc)

        # Two real events via query -> processed.
        request = ReplayRequest(mode=ReplayMode.STRICT)
        results = [r async for r in engine.replay(request)]
        assert len(results) == 2
        assert acc.counters().replay_processed == 2

        # One missing event via correlation_ids -> rejected.
        request_missing = ReplayRequest(
            correlation_ids=["ghost-id"],
            mode=ReplayMode.STRICT,
        )
        _ = [r async for r in engine.replay(request_missing)]
        assert acc.counters().replay_rejected == 1

    async def test_no_accounting_is_graceful(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """ReplayEngine works correctly when accounting is None (default)."""
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)  # No accounting
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]

        assert len(results) == 1
        assert results[0].status == "passed"

    async def test_dry_run_increments_processed(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN replay increments replay_processed (event was replayed)."""
        await temp_storage.append(sample_event)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        acc = RuntimeAccounting()
        engine = make_engine(temp_storage, pipeline=pipeline, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.replay_processed == 1
        assert c.replay_rejected == 0

    async def test_other_counters_unchanged_by_replay(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Replay accounting does not touch unrelated counters."""
        await temp_storage.append(sample_event)

        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        engine = make_engine(temp_storage, accounting=acc)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        _ = [r async for r in engine.replay(request)]

        c = acc.counters()
        assert c.inbound_accepted == 1
        assert c.outbound_attempts == 1
        assert c.outbound_delivered == 0
        assert c.outbound_failed == 0
        assert c.loop_prevented == 0
        assert c.capacity_rejections == 0

    # ------------------------------------------------------------------
    # Observability snapshot (from TestStressReplay)
    # ------------------------------------------------------------------

    async def test_replay_observability_snapshot(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """Diagnostician snapshot reflects replay failures across categories."""
        from medre.core.observability.metrics import Diagnostician

        # Store a good event and an unregistered kind event
        await temp_storage.append(sample_event)
        bad_event = CanonicalEvent(
            event_id="obs-bad-001",
            event_kind="unknown.type",
            schema_version=1,
            timestamp=sample_event.timestamp,
            source_adapter="test",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "bad"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(bad_event)

        diag = Diagnostician()
        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        # STRICT replay -- one passes, one fails (unregistered kind)
        from medre.core.engine.replay.types import collect_replay_state

        await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.STRICT)),
        )
        snap = diag.snapshot()
        assert len(snap["replay_downgrades"]) > 0

        # RE_RENDER with a pipeline that raises on render
        bad_pipeline = AsyncMock()
        bad_pipeline.transform_event = AsyncMock(return_value=sample_event)
        bad_pipeline.render_event = AsyncMock(
            side_effect=RuntimeError("renderer crashed"),
        )
        engine2 = make_engine(temp_storage, pipeline=bad_pipeline)
        engine2._diagnostician = diag

        await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.RE_RENDER)),
        )
        snap2 = diag.snapshot()
        assert len(snap2["renderer_failures"]) > 0
