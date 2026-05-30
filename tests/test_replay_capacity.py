"""Replay capacity: bulk replay determinism, mixed-status aggregation.

Stress-style replay tests that verify deterministic ordering under load,
mixed-status aggregation across many events, and side-effect guarantees
during bulk operations.
"""

from __future__ import annotations

from datetime import timedelta

from medre.core.engine.replay import (
    ReplayMode,
    ReplayRequest,
    collect_replay_state,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering import RenderingPipeline
from medre.core.routing import Router
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import (
    StubPipeline,
    make_engine,
    make_events,
)


class TestStressReplay:
    """Bulk replay determinism, mixed-status aggregation, and observability."""

    async def test_stress_replay_50_events_strict(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """STRICT replay of 50 events: all pass, deterministic order."""
        events = make_events(50, sample_event)
        for e in events:
            await temp_storage.append(e)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 50

        # All passed
        assert all(r.status == "passed" for r in results)

        # Deterministic order: sequential by timestamp
        result_ids = [r.event_id for r in results]
        assert result_ids == [f"stress-{i:04d}" for i in range(50)]

        # Replay again -- identical ordering
        results2 = [r async for r in engine.replay(request)]
        assert [r.event_id for r in results2] == result_ids

    async def test_stress_replay_50_events_re_render(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """RE_RENDER replay of 50 events: store + render for each, no mutation."""
        events = make_events(50, sample_event)
        for e in events:
            await temp_storage.append(e)

        pipeline = StubPipeline(rendering_pipeline=rendering_pipeline)
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 100  # 50 store + 50 render

        # Every other result is a store pass
        store_results = results[::2]
        render_results = results[1::2]
        assert all(r.stage == "store" for r in store_results)
        assert all(r.stage == "render" for r in render_results)
        assert all(r.status == "passed" for r in store_results)
        assert all(r.status == "passed" for r in render_results)

        # Rendered outputs contain the correct text
        for i, r in enumerate(render_results):
            assert f"event {i}" in (r.output.payload.get("text", ""))

        # No storage mutation
        all_events = [e async for e in temp_storage.query(EventFilter())]
        assert len(all_events) == 50

    async def test_replay_mixed_statuses_aggregation(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Mixed events: 5 good, 3 unregistered kind, 2 missing -> correct state."""
        from medre.core.observability.metrics import Diagnostician

        # 5 good events
        good_events = make_events(5, sample_event)
        for e in good_events:
            await temp_storage.append(e)

        # 3 unregistered kind events
        for i in range(3):
            bad = CanonicalEvent(
                event_id=f"bad-{i}",
                event_kind=f"unknown.type_{i}",
                schema_version=1,
                timestamp=sample_event.timestamp + timedelta(seconds=100 + i),
                source_adapter="test",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "bad"},
                metadata=EventMetadata(),
            )
            await temp_storage.append(bad)

        diag = Diagnostician()
        engine = make_engine(temp_storage)
        engine._diagnostician = diag

        # Replay all events
        state = await collect_replay_state(
            engine.replay(ReplayRequest(mode=ReplayMode.STRICT)),
        )
        assert state.events_processed == 8
        assert state.events_passed == 5
        assert state.events_failed == 3
        assert len(state.errors) == 3

        # Now replay 2 missing IDs via correlation_ids
        request = ReplayRequest(
            correlation_ids=["stress-0049", "nonexistent-a", "nonexistent-b"],
            mode=ReplayMode.STRICT,
        )
        state2 = await collect_replay_state(engine.replay(request))
        # stress-0049 doesn't exist (we only have stress-0000..0004)
        assert state2.events_processed == 3
        assert state2.events_failed == 3

    async def test_stress_replay_dry_run_no_side_effects(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
        router_with_routes: Router,
        rendering_pipeline: RenderingPipeline,
    ) -> None:
        """DRY_RUN of 20 events: all stages except deliver, no storage mutation."""
        events = make_events(20, sample_event)
        for e in events:
            await temp_storage.append(e)

        pipeline = StubPipeline(
            router=router_with_routes,
            rendering_pipeline=rendering_pipeline,
        )
        engine = make_engine(temp_storage, pipeline=pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        count_before = len([e async for e in temp_storage.query(EventFilter())])
        results = [r async for r in engine.replay(request)]
        count_after = len([e async for e in temp_storage.query(EventFilter())])

        # 20 events x 5 stages = 100 results
        assert len(results) == 100
        assert count_before == count_after == 20

        # Every 5th result (deliver stage) is skipped
        deliver_results = results[4::5]
        assert all(r.stage == "deliver" for r in deliver_results)
        assert all(r.status == "skipped" for r in deliver_results)
