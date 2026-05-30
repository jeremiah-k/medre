"""Replay source, run_id, receipts, native ref traceability.

Tests collect_replay_state aggregation, ReplayState status recording,
lineage tracking, and stage resolution. This file is the home for both
existing traceability tests and new replay bridge condition tests.
"""

from __future__ import annotations

from medre.core.engine.replay.types import (
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayState,
    collect_replay_state,
)
from medre.core.events import CanonicalEvent
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import (
    make_engine,
    make_second_event,
)


class TestReplayTraceability:
    """Replay state aggregation, lineage, and stage resolution."""

    # ------------------------------------------------------------------
    # collect_replay_state aggregation
    # ------------------------------------------------------------------

    async def test_collect_replay_state_aggregates(
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

    # ------------------------------------------------------------------
    # ReplayState recording
    # ------------------------------------------------------------------

    def test_replay_state_records_all_statuses(self) -> None:
        """ReplayState correctly counts passed, skipped, failed, error."""
        state = ReplayState()

        state.record(ReplayResult(event_id="a", stage="store", status="passed"))
        state.record(ReplayResult(event_id="b", stage="store", status="skipped"))
        state.record(
            ReplayResult(event_id="c", stage="store", status="failed", error="bad")
        )
        state.record(
            ReplayResult(event_id="d", stage="store", status="error", error="boom")
        )

        assert state.events_processed == 4
        assert state.events_passed == 1
        assert state.events_skipped == 1
        assert state.events_failed == 2
        assert state.errors == ["bad", "boom"]

    def test_replay_state_lineage_tracking(self) -> None:
        """ReplayState updates current_lineage from results."""
        state = ReplayState()

        state.record(
            ReplayResult(
                event_id="a",
                stage="store",
                status="passed",
                lineage=["parent-1"],
            )
        )
        assert state.current_lineage == ["parent-1"]

        state.record(
            ReplayResult(
                event_id="b",
                stage="store",
                status="passed",
                lineage=["parent-2", "parent-3"],
            )
        )
        assert state.current_lineage == ["parent-2", "parent-3"]

    # ------------------------------------------------------------------
    # Stage resolution
    # ------------------------------------------------------------------

    def test_resolve_stages_all_modes(self) -> None:
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

    def test_resolve_stages_with_target_stages(self) -> None:
        """target_stages intersects with mode-allowed stages."""
        from medre.core.engine.replay.helpers import _resolve_stages

        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            target_stages=["store", "render"],
        )
        stages = _resolve_stages(request)
        assert stages == ("store", "render")  # ordered by mode definition

    def test_resolve_stages_target_stages_subset(self) -> None:
        """target_stages only returns stages allowed by the mode."""
        from medre.core.engine.replay.helpers import _resolve_stages

        # STRICT only allows "store"; requesting "render" is a no-op
        request = ReplayRequest(
            mode=ReplayMode.STRICT,
            target_stages=["render"],
        )
        stages = _resolve_stages(request)
        assert stages == ()
