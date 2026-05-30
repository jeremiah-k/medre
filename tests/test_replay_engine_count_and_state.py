"""ReplayEngine count_matching, deterministic ordering, state aggregation,
and stage resolution.
"""

from __future__ import annotations

from datetime import timedelta

from medre.core.engine.replay.types import (
    ReplayMode,
    ReplayRequest,
    collect_replay_state,
)
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import make_engine, make_second_event

# ===================================================================
# count_matching
# ===================================================================


class TestCountMatching:
    """Verify count_matching returns correct counts without replaying."""

    async def test_basic_count(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching returns correct count without replaying."""
        await temp_storage.append(sample_event)

        second = make_second_event(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        count = await engine.count_matching(request)
        assert count == 2

        # Also verify a filtered count
        filtered_request = ReplayRequest(
            event_kinds=["message.created"],
            source_adapters=["nonexistent"],
            mode=ReplayMode.STRICT,
        )
        filtered_count = await engine.count_matching(filtered_request)
        assert filtered_count == 0

    async def test_with_correlation_ids(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching with correlation_ids counts both existing and missing.

        Missing correlation IDs are counted to stay consistent with
        replay(), which emits "failed" results for them.  This makes
        count_matching a reliable work estimator.
        """
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            correlation_ids=[sample_event.event_id, "nonexistent-id"],
            mode=ReplayMode.STRICT,
        )

        count = await engine.count_matching(request)
        assert count == 2  # sample_event + missing ID

    async def test_with_filters(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """count_matching applies time, kind, and adapter filters."""
        evt_old = CanonicalEvent(
            event_id="old-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=sample_event.timestamp - timedelta(hours=2),
            source_adapter="adapter_alpha",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "old event"},
            metadata=EventMetadata(),
        )
        evt_presence = CanonicalEvent(
            event_id="presence-001",
            event_kind="presence.changed",
            schema_version=1,
            timestamp=sample_event.timestamp + timedelta(hours=1),
            source_adapter="adapter_beta",
            source_transport_id="node-456",
            source_channel_id="ch-1",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"status": "online"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(evt_old)
        await temp_storage.append(evt_presence)
        await temp_storage.append(sample_event)

        engine = make_engine(temp_storage)

        # All events
        count_all = await engine.count_matching(
            ReplayRequest(mode=ReplayMode.STRICT),
        )
        assert count_all == 3

        # Filter by event kind
        count_kind = await engine.count_matching(
            ReplayRequest(
                event_kinds=["message.created"],
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_kind == 2  # evt_old + sample_event

        # Filter by source adapter
        count_adapter = await engine.count_matching(
            ReplayRequest(
                source_adapters=["adapter_beta"],
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_adapter == 1

        # Filter by time window around sample_event
        count_time = await engine.count_matching(
            ReplayRequest(
                time_start=sample_event.timestamp - timedelta(minutes=1),
                time_end=sample_event.timestamp + timedelta(minutes=1),
                mode=ReplayMode.STRICT,
            ),
        )
        assert count_time == 1  # Only sample_event in the window


# ===================================================================
# Deterministic ordering
# ===================================================================


class TestDeterministicOrdering:
    """Verify replay produces consistent, deterministic results."""

    async def test_results_are_ordered_deterministically(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Multiple events produce results in consistent order."""
        second = make_second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2

        # Run again -- order must be identical
        results2 = [r async for r in engine.replay(request)]
        assert [r.event_id for r in results] == [r.event_id for r in results2]


# ===================================================================
# Stage resolution
# ===================================================================


class TestResolveStages:
    """Verify _resolve_stages returns correct stages for each mode."""

    def test_all_modes(self) -> None:
        """_resolve_stages returns correct stages for each mode."""
        from medre.core.engine.replay.helpers import _resolve_stages

        strict = _resolve_stages(ReplayRequest(mode=ReplayMode.STRICT))
        assert strict == ("store",)

        re_render = _resolve_stages(ReplayRequest(mode=ReplayMode.RE_RENDER))
        assert re_render == ("store", "render")

        re_route = _resolve_stages(ReplayRequest(mode=ReplayMode.RE_ROUTE))
        assert re_route == ("store", "route", "plan")

        best = _resolve_stages(ReplayRequest(mode=ReplayMode.BEST_EFFORT))
        assert best == ("store", "route", "plan", "render", "deliver")

        dry = _resolve_stages(ReplayRequest(mode=ReplayMode.DRY_RUN))
        assert dry == ("store", "route", "plan", "render", "deliver")

    def test_with_target_stages(self) -> None:
        """target_stages intersects with mode-allowed stages."""
        from medre.core.engine.replay.helpers import _resolve_stages

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            target_stages=["store", "render"],
        )
        stages = _resolve_stages(request)
        assert stages == ("store", "render")  # ordered by mode definition

    def test_target_stages_subset(self) -> None:
        """target_stages only returns stages allowed by the mode."""
        from medre.core.engine.replay.helpers import _resolve_stages

        # STRICT only allows "store"; requesting "render" is a no-op
        request = ReplayRequest(
            mode=ReplayMode.STRICT,
            target_stages=["render"],
        )
        stages = _resolve_stages(request)
        assert stages == ()


# ===================================================================
# collect_replay_state aggregation
# ===================================================================


class TestCollectReplayState:
    """Verify collect_replay_state correctly aggregates results."""

    async def test_aggregates_multi_event(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """collect_replay_state correctly aggregates multi-event results."""
        second = make_second_event(sample_event)
        await temp_storage.append(sample_event)
        await temp_storage.append(second)

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.STRICT)

        state = await collect_replay_state(engine.replay(request))

        assert state.events_processed == 2
        assert state.events_passed == 2
        assert state.events_failed == 0
        assert state.events_skipped == 0
