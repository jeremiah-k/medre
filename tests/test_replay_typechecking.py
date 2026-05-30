"""TYPE_CHECKING import coverage and basic ReplayEngine construction.

Verifies that the guarded imports on lines 97-100 of replay.py
(Diagnostician, RuntimeAccounting, CapacityController) are compatible
with ReplayEngine's constructor when mocked, and exercises basic
construction of the public dataclasses and enums.
"""

from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import MagicMock

import pytest

from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.summary import ReplaySummary, _build_summary
from medre.core.engine.replay.types import (
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    ReplayRouteAttribution,
    ReplayState,
    collect_replay_state,
)

# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------


class TestModuleImport:
    """Verify the replay module loads cleanly."""

    def test_import_replay_module(self) -> None:
        """Importing medre.core.engine.replay submodules should not raise."""
        import medre.core.engine.replay.engine as replay_engine
        import medre.core.engine.replay.summary as replay_summary
        import medre.core.engine.replay.types as replay_types

        assert hasattr(replay_engine, "ReplayEngine")
        assert hasattr(replay_types, "ReplayMode")
        assert hasattr(replay_types, "ReplayRequest")
        assert hasattr(replay_types, "ReplayResult")
        assert hasattr(replay_types, "ReplayState")
        assert hasattr(replay_types, "collect_replay_state")
        assert hasattr(replay_summary, "ReplaySummary")
        assert hasattr(replay_summary, "_build_summary")

    def test_engine_replay_not_in_storage(self) -> None:
        """medre.core.storage must not re-export replay runtime symbols.

        Replay orchestration is owned by core.engine, not core.storage.
        """
        import medre.core.storage as storage_mod

        for attr in (
            "ReplayEngine",
            "ReplayMode",
            "ReplayRequest",
            "ReplayResult",
            "ReplayRouteAttribution",
            "ReplaySummary",
            "collect_replay_summary",
        ):
            assert not hasattr(
                storage_mod, attr
            ), f"storage must not re-export {attr}; replay lives in core.engine"

    def test_engine_package_does_not_re_export_replay_symbols(self) -> None:
        """medre.core.engine must not re-export replay runtime symbols.

        Replay runtime lives in medre.core.engine.replay and should be
        imported explicitly, not via the engine package root.
        """
        import medre.core.engine as engine_mod

        for attr in (
            "ReplayEngine",
            "ReplayMode",
            "ReplayRequest",
            "ReplayResult",
            "ReplayRouteAttribution",
            "ReplaySummary",
            "collect_replay_summary",
        ):
            assert not hasattr(
                engine_mod, attr
            ), f"engine root must not re-export {attr}; import from medre.core.engine.replay instead"


# ---------------------------------------------------------------------------
# ReplayMode enum
# ---------------------------------------------------------------------------


class TestReplayMode:
    """ReplayMode enum covers all five modes."""

    @pytest.mark.parametrize(
        "name",
        ["STRICT", "RE_RENDER", "RE_ROUTE", "BEST_EFFORT", "DRY_RUN"],
    )
    def test_mode_exists(self, name: str) -> None:
        mode = ReplayMode[name]
        assert (
            mode.value == name.lower() or mode.value == name.replace("_", ".").lower()
        )


# ---------------------------------------------------------------------------
# ReplayRequest
# ---------------------------------------------------------------------------


class TestReplayRequest:
    """ReplayRequest construction with various modes."""

    def test_default_mode_is_strict(self) -> None:
        req = ReplayRequest()
        assert req.mode is ReplayMode.STRICT
        assert req.limit == 1000
        assert req.run_id == ""

    def test_replay_request_constructs_with_all_modes(self) -> None:
        for mode in ReplayMode:
            req = ReplayRequest(mode=mode, event_kinds=["message.created"])
            assert req.mode is mode
            assert req.event_kinds == ["message.created"]

    def test_replay_request_with_filters(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        req = ReplayRequest(
            time_start=now,
            time_end=now,
            event_kinds=["message.created"],
            source_adapters=["fake_transport"],
            target_stages=["store", "render"],
            correlation_ids=["evt-001"],
            mode=ReplayMode.RE_RENDER,
            limit=10,
            target_adapters=["matrix"],
            route_ids=("route-a",),
            run_id="run-42",
        )
        assert req.time_start is now
        assert req.correlation_ids == ["evt-001"]
        assert req.target_adapters == ["matrix"]
        assert req.route_ids == ("route-a",)
        assert req.run_id == "run-42"


# ---------------------------------------------------------------------------
# ReplayResult
# ---------------------------------------------------------------------------


class TestReplayResult:
    """ReplayResult with different statuses."""

    @pytest.mark.parametrize("status", ["passed", "skipped", "failed", "error"])
    def test_replay_result_constructs_with_status(self, status: str) -> None:
        from typing import Literal, cast

        result = ReplayResult(
            event_id="evt-001",
            stage="store",
            status=cast(Literal["passed", "skipped", "failed", "error"], status),
        )
        assert result.event_id == "evt-001"
        assert result.stage == "store"
        assert result.status == status
        assert result.output is None
        assert result.error is None
        assert result.duration_ms == 0.0
        assert result.lineage == []
        assert result.route_attribution is None

    def test_replay_result_with_error(self) -> None:
        result = ReplayResult(
            event_id="evt-002",
            stage="deliver",
            status="error",
            error="connection refused",
            duration_ms=42.5,
            lineage=["evt-000", "evt-002"],
        )
        assert result.error == "connection refused"
        assert result.duration_ms == 42.5
        assert result.lineage == ["evt-000", "evt-002"]


# ---------------------------------------------------------------------------
# ReplayState
# ---------------------------------------------------------------------------


class TestReplayState:
    """ReplayState.record() tracks results correctly."""

    def test_initial_state(self) -> None:
        state = ReplayState()
        assert state.events_processed == 0
        assert state.events_passed == 0
        assert state.events_skipped == 0
        assert state.events_failed == 0
        assert state.errors == []

    def test_record_passed(self) -> None:
        state = ReplayState()
        state.record(ReplayResult(event_id="e1", stage="store", status="passed"))
        assert state.events_processed == 1
        assert state.events_passed == 1
        assert state.events_skipped == 0
        assert state.events_failed == 0

    def test_record_skipped(self) -> None:
        state = ReplayState()
        state.record(ReplayResult(event_id="e1", stage="deliver", status="skipped"))
        assert state.events_processed == 1
        assert state.events_skipped == 1

    def test_record_failed_with_error(self) -> None:
        state = ReplayState()
        state.record(
            ReplayResult(event_id="e1", stage="render", status="error", error="boom")
        )
        assert state.events_failed == 1
        assert state.errors == ["boom"]

    def test_record_updates_lineage(self) -> None:
        state = ReplayState()
        state.record(
            ReplayResult(
                event_id="e1", stage="store", status="passed", lineage=["a", "b"]
            )
        )
        assert state.current_lineage == ["a", "b"]

    def test_mixed_results(self) -> None:
        state = ReplayState()
        state.record(ReplayResult(event_id="e1", stage="store", status="passed"))
        state.record(ReplayResult(event_id="e1", stage="render", status="passed"))
        state.record(ReplayResult(event_id="e2", stage="store", status="skipped"))
        state.record(
            ReplayResult(event_id="e3", stage="deliver", status="error", error="fail")
        )
        assert state.events_processed == 4
        assert state.events_passed == 2
        assert state.events_skipped == 1
        assert state.events_failed == 1
        assert state.errors == ["fail"]


# ---------------------------------------------------------------------------
# ReplayRouteAttribution
# ---------------------------------------------------------------------------


class TestReplayRouteAttribution:
    """ReplayRouteAttribution is frozen and serialisable."""

    def test_default_values(self) -> None:
        attr = ReplayRouteAttribution()
        assert attr.is_replay is True
        assert attr.route_ids == ()

    def test_to_dict(self) -> None:
        attr = ReplayRouteAttribution(
            route_ids=("r1",),
            source_adapter="matrix",
            target_adapters=("mesh",),
            replay_mode="strict",
            run_id="run-1",
        )
        d = attr.to_dict()
        assert d["is_replay"] is True
        assert d["route_ids"] == ["r1"]
        assert d["source_adapter"] == "matrix"
        assert d["target_adapters"] == ["mesh"]


# ---------------------------------------------------------------------------
# ReplayEngine construction with mocks
# ---------------------------------------------------------------------------


class TestReplayEngineConstruction:
    """ReplayEngine accepts mock TYPE_CHECKING'd collaborators."""

    def test_engine_constructs_with_storage_only(self) -> None:
        storage = MagicMock()
        engine = ReplayEngine(storage=storage)
        assert engine is not None

    def test_engine_constructs_with_all_mock_params(self) -> None:
        """Verify TYPE_CHECKING'd types are compatible via mocks.

        Diagnostician, RuntimeAccounting, and CapacityController are
        imported only under TYPE_CHECKING (lines 97-100).  Passing mock
        objects validates runtime compatibility without importing the
        actual modules.
        """
        storage = MagicMock()
        pipeline = MagicMock()
        event_bus = MagicMock()
        diagnostician = MagicMock()  # stands in for Diagnostician
        capacity_controller = MagicMock()  # stands in for CapacityController
        accounting = MagicMock()  # stands in for RuntimeAccounting

        engine = ReplayEngine(
            storage=storage,
            pipeline=pipeline,
            event_bus=event_bus,
            diagnostician=diagnostician,
            capacity_controller=capacity_controller,
            accounting=accounting,
        )
        assert engine is not None

    def test_engine_set_capacity_controller(self) -> None:
        storage = MagicMock()
        cc = MagicMock()
        engine = ReplayEngine(storage=storage)
        engine.set_capacity_controller(cc)

    def test_engine_accepts_none_optionals(self) -> None:
        storage = MagicMock()
        engine = ReplayEngine(
            storage=storage,
            pipeline=None,
            event_bus=None,
            diagnostician=None,
            capacity_controller=None,
            accounting=None,
        )
        assert engine is not None


# ---------------------------------------------------------------------------
# collect_replay_state (async)
# ---------------------------------------------------------------------------


class TestCollectReplayState:
    """collect_replay_state consumes an async iterator of results."""

    async def test_empty_iterator(self) -> None:
        async def _empty() -> AsyncIterator[ReplayResult]:
            return
            yield  # noqa: unreachable – makes this an async generator

        state = await collect_replay_state(_empty())
        assert state.events_processed == 0

    async def test_single_result(self) -> None:
        r = ReplayResult(event_id="e1", stage="store", status="passed")

        async def _gen() -> AsyncIterator[ReplayResult]:
            yield r

        state = await collect_replay_state(_gen())
        assert state.events_processed == 1
        assert state.events_passed == 1


# ---------------------------------------------------------------------------
# _build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """_build_summary produces a ReplaySummary from materialised results."""

    def test_empty_results(self) -> None:
        summary = _build_summary([], events_scanned=0, elapsed_ms=0.0)
        assert isinstance(summary, ReplaySummary)
        assert summary.events_replayed == 0
        assert summary.events_scanned == 0

    def test_with_results(self) -> None:
        results = [
            ReplayResult(event_id="e1", stage="store", status="passed"),
            ReplayResult(event_id="e1", stage="render", status="passed"),
            ReplayResult(event_id="e2", stage="deliver", status="error", error="boom"),
        ]
        summary = _build_summary(
            results,
            events_scanned=3,
            elapsed_ms=100.0,
            mode=ReplayMode.BEST_EFFORT,
            run_id="run-99",
        )
        assert summary.events_replayed == 3
        assert summary.events_scanned == 3
        assert summary.elapsed_ms == 100.0
        assert summary.by_status["passed"] == 2
        assert summary.by_status["error"] == 1
        assert "boom" in summary.errors
        assert summary.run_id == "run-99"
