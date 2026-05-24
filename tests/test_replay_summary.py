"""Track 3: ReplaySummary observability hardening tests.

Tests the immutable ReplaySummary model, collect_replay_summary helper,
_build_summary construction, to_dict() determinism, JSON-serialisation,
and integration with collect_replay_state.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest

from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import (
    ReplayMode,
    ReplayResult,
    ReplayState,
    ReplaySummary,
    _build_summary,
    collect_replay_state,
    collect_replay_summary,
)
from medre.core.storage.sqlite import SQLiteStorage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    event_id: str = "evt-001",
    stage: str = "store",
    status: Literal["passed", "skipped", "failed", "error"] = "passed",
    error: str | None = None,
    output: Any = None,
) -> ReplayResult:
    """Create a ReplayResult with sensible defaults."""
    return ReplayResult(
        event_id=event_id,
        stage=stage,
        status=status,
        error=error,
        output=output,
    )


async def _aiter(items: list[ReplayResult]):
    """Wrap a list as an async iterator."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# ReplaySummary immutability
# ---------------------------------------------------------------------------


class TestReplaySummaryImmutability:
    """ReplaySummary is frozen – attribute assignment raises."""

    def test_frozen_prevents_mutation(self) -> None:
        summary = ReplaySummary()
        with pytest.raises(AttributeError):
            summary.events_replayed = 42  # type: ignore[misc]

    def test_frozen_dict_fields_are_still_mutable_values(self) -> None:
        """Frozen dataclass doesn't deep-freeze containers, but the
        *reference* is immutable.  Users should treat the object as
        read-only."""
        summary = ReplaySummary()
        # The dict itself can be mutated (Python frozen dataclass caveat)
        # but the field reassignment is blocked.
        with pytest.raises(AttributeError):
            summary.by_status = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# to_dict() structure and determinism
# ---------------------------------------------------------------------------


class TestReplaySummaryToDict:
    """to_dict() returns a deterministic, JSON-safe mapping."""

    def test_default_summary_to_dict(self) -> None:
        summary = ReplaySummary()
        d = summary.to_dict()
        assert d == {
            "by_route": {},
            "by_stage": {},
            "by_status": {"error": 0, "failed": 0, "passed": 0, "skipped": 0},
            "elapsed_ms": 0.0,
            "errors": [],
            "events_replayed": 0,
            "events_scanned": 0,
            "failure_count": 0,
            "mode": None,
            "route_resolution_count": 0,
            "run_id": "",
            "skipped_count": 0,
        }

    def test_json_dumps_sort_keys(self) -> None:
        """json.dumps(summary.to_dict(), sort_keys=True) works."""
        summary = ReplaySummary(
            events_scanned=10,
            events_replayed=8,
            skipped_count=1,
            failure_count=2,
            route_resolution_count=3,
            elapsed_ms=1234.5,
            by_status={"passed": 5, "skipped": 1, "failed": 1, "error": 1},
            by_stage={"store": 4, "route": 2},
            errors=("err1", "err2"),
            mode=ReplayMode.STRICT,
        )
        serialized = json.dumps(summary.to_dict(), sort_keys=True)
        deserialized = json.loads(serialized)
        assert deserialized["events_scanned"] == 10
        assert deserialized["events_replayed"] == 8
        assert deserialized["elapsed_ms"] == 1234.5
        assert deserialized["mode"] == "strict"
        assert deserialized["errors"] == ["err1", "err2"]

    def test_mode_serialised_as_string(self) -> None:
        for mode in ReplayMode:
            summary = ReplaySummary(mode=mode)
            d = summary.to_dict()
            assert d["mode"] == mode.value

    def test_by_status_always_has_four_keys(self) -> None:
        """Even when only some statuses appear in results."""
        results = [
            _result(status="passed"),
            _result(status="passed"),
        ]
        summary = _build_summary(results)
        d = summary.to_dict()
        assert set(d["by_status"].keys()) == {"passed", "skipped", "failed", "error"}
        assert d["by_status"]["passed"] == 2
        assert d["by_status"]["skipped"] == 0

    def test_deterministic_ordering(self) -> None:
        """to_dict() produces the same structure regardless of input order."""
        r1 = _result(stage="route", status="passed")
        r2 = _result(stage="store", status="passed")
        summary = _build_summary([r1, r2])
        d = summary.to_dict()
        # by_stage should be sorted
        assert list(d["by_stage"].keys()) == ["route", "store"]


# ---------------------------------------------------------------------------
# _build_summary from results
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """_build_summary correctly aggregates result lists."""

    def test_all_passed(self) -> None:
        results = [
            _result(event_id="a", stage="store", status="passed"),
            _result(event_id="a", stage="render", status="passed"),
            _result(event_id="b", stage="store", status="passed"),
        ]
        summary = _build_summary(results, events_scanned=2, mode=ReplayMode.RE_RENDER)

        assert summary.events_scanned == 2
        assert summary.events_replayed == 3
        assert summary.skipped_count == 0
        assert summary.failure_count == 0
        assert summary.by_status == {"passed": 3, "skipped": 0, "failed": 0, "error": 0}
        assert summary.by_stage == {"store": 2, "render": 1}
        assert summary.errors == ()
        assert summary.mode == ReplayMode.RE_RENDER

    def test_partial_failures_and_errors(self) -> None:
        results = [
            _result(stage="store", status="passed"),
            _result(stage="route", status="failed", error="no routes"),
            _result(stage="render", status="error", error="crash"),
            _result(stage="deliver", status="skipped"),
        ]
        summary = _build_summary(results)

        assert summary.events_replayed == 4
        assert summary.skipped_count == 1
        assert summary.failure_count == 2  # failed + error
        assert summary.by_status["passed"] == 1
        assert summary.by_status["failed"] == 1
        assert summary.by_status["error"] == 1
        assert summary.by_status["skipped"] == 1
        assert summary.errors == ("no routes", "crash")

    def test_empty_results(self) -> None:
        summary = _build_summary([], events_scanned=0, elapsed_ms=5.0)

        assert summary.events_scanned == 0
        assert summary.events_replayed == 0
        assert summary.skipped_count == 0
        assert summary.failure_count == 0
        assert summary.route_resolution_count == 0
        assert summary.elapsed_ms == 5.0
        assert summary.by_status == {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
        assert summary.by_stage == {}
        assert summary.errors == ()

    def test_stage_and_status_counts(self) -> None:
        results = [
            _result(event_id="e1", stage="store", status="passed"),
            _result(
                event_id="e1",
                stage="route",
                status="passed",
                output=[("r", ["t1", "t2"])],
            ),
            _result(event_id="e1", stage="plan", status="passed"),
            _result(event_id="e2", stage="store", status="failed", error="not found"),
            _result(
                event_id="e2", stage="route", status="skipped", error="upstream failed"
            ),
        ]
        summary = _build_summary(results, events_scanned=3)

        assert summary.by_stage == {"store": 2, "route": 2, "plan": 1}
        assert summary.by_status["passed"] == 3
        assert summary.by_status["failed"] == 1
        assert summary.by_status["skipped"] == 1
        assert summary.events_scanned == 3

    def test_elapsed_ms_default_zero(self) -> None:
        summary = _build_summary([])
        assert summary.elapsed_ms == 0.0

    def test_elapsed_ms_provided(self) -> None:
        summary = _build_summary([], elapsed_ms=999.9)
        assert summary.elapsed_ms == 999.9

    def test_route_resolution_count_from_route_stage(self) -> None:
        """route-stage results with passed status and non-empty output count."""
        results = [
            _result(stage="store", status="passed"),
            _result(stage="route", status="passed", output=[("route", ["target"])]),
        ]
        summary = _build_summary(results)
        assert summary.route_resolution_count == 1

    def test_route_resolution_count_zero_when_route_failed(self) -> None:
        results = [
            _result(stage="route", status="failed", output=[]),
        ]
        summary = _build_summary(results)
        assert summary.route_resolution_count == 0

    def test_route_resolution_count_zero_when_no_route_stage(self) -> None:
        results = [
            _result(stage="store", status="passed"),
        ]
        summary = _build_summary(results)
        assert summary.route_resolution_count == 0

    def test_error_truncation(self) -> None:
        """Errors beyond _MAX_SUMMARY_ERRORS are dropped."""
        from medre.core.storage.replay import _MAX_SUMMARY_ERRORS

        results = [
            _result(event_id=f"e{i}", stage="store", status="error", error=f"error {i}")
            for i in range(_MAX_SUMMARY_ERRORS + 10)
        ]
        summary = _build_summary(results)
        assert len(summary.errors) == _MAX_SUMMARY_ERRORS

    def test_error_message_length_capped(self) -> None:
        from medre.core.storage.replay import _MAX_ERROR_LENGTH

        long_error = "x" * (_MAX_ERROR_LENGTH + 100)
        results = [_result(status="error", error=long_error)]
        summary = _build_summary(results)
        assert len(summary.errors[0]) == _MAX_ERROR_LENGTH

    def test_mode_none_by_default(self) -> None:
        summary = _build_summary([])
        assert summary.mode is None

    def test_mode_preserved(self) -> None:
        summary = _build_summary([], mode=ReplayMode.BEST_EFFORT)
        assert summary.mode == ReplayMode.BEST_EFFORT


# ---------------------------------------------------------------------------
# collect_replay_summary async helper
# ---------------------------------------------------------------------------


class TestCollectReplaySummary:
    """collect_replay_summary works as an async consumer."""

    async def test_basic_collection(self) -> None:
        results = [
            _result(event_id="a", stage="store", status="passed"),
            _result(event_id="b", stage="store", status="passed"),
        ]
        summary = await collect_replay_summary(_aiter(results))
        assert summary.events_replayed == 2
        assert summary.events_scanned == 2  # derived from distinct event_ids
        assert summary.failure_count == 0

    async def test_events_scanned_override(self) -> None:
        """When events_scanned is provided, it is used instead of deriving."""
        results = [_result(event_id="a", stage="store", status="passed")]
        summary = await collect_replay_summary(
            _aiter(results),
            events_scanned=100,
        )
        assert summary.events_scanned == 100
        assert summary.events_replayed == 1

    async def test_elapsed_ms_provided(self) -> None:
        results = [_result(status="passed")]
        summary = await collect_replay_summary(
            _aiter(results),
            elapsed_ms=42.5,
        )
        assert summary.elapsed_ms == 42.5

    async def test_elapsed_ms_default(self) -> None:
        results = [_result(status="passed")]
        summary = await collect_replay_summary(_aiter(results))
        assert summary.elapsed_ms == 0.0

    async def test_mode_provided(self) -> None:
        results = [_result(status="passed")]
        summary = await collect_replay_summary(
            _aiter(results),
            mode=ReplayMode.DRY_RUN,
        )
        assert summary.mode == ReplayMode.DRY_RUN

    async def test_empty_results(self) -> None:
        summary = await collect_replay_summary(_aiter([]))
        assert summary.events_replayed == 0
        assert summary.events_scanned == 0
        assert summary.failure_count == 0

    async def test_mixed_statuses(self) -> None:
        results = [
            _result(event_id="a", stage="store", status="passed"),
            _result(event_id="a", stage="render", status="passed"),
            _result(event_id="b", stage="store", status="failed", error="bad"),
            _result(event_id="c", stage="store", status="skipped"),
            _result(event_id="d", stage="store", status="error", error="boom"),
        ]
        summary = await collect_replay_summary(
            _aiter(results),
            events_scanned=4,
            elapsed_ms=100.0,
            mode=ReplayMode.RE_RENDER,
        )

        assert summary.events_scanned == 4
        assert summary.events_replayed == 5
        assert summary.skipped_count == 1
        assert summary.failure_count == 2
        assert summary.by_status["passed"] == 2
        assert summary.by_status["failed"] == 1
        assert summary.by_status["error"] == 1
        assert summary.by_status["skipped"] == 1
        assert summary.errors == ("bad", "boom")
        assert summary.elapsed_ms == 100.0
        assert summary.mode == ReplayMode.RE_RENDER

    async def test_distinct_event_id_derivation(self) -> None:
        """events_scanned defaults to distinct event_ids."""
        results = [
            _result(event_id="a", stage="store", status="passed"),
            _result(event_id="a", stage="render", status="passed"),
            _result(event_id="b", stage="store", status="passed"),
        ]
        summary = await collect_replay_summary(_aiter(results))
        assert summary.events_scanned == 2  # distinct: "a" and "b"
        assert summary.events_replayed == 3

    async def test_returns_frozen_summary(self) -> None:
        results = [_result(status="passed")]
        summary = await collect_replay_summary(_aiter(results))
        with pytest.raises(AttributeError):
            summary.events_replayed = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Direct constructor scalar defaults: collect_replay_state unchanged
# ---------------------------------------------------------------------------


class TestCollectReplayStateBackwardCompat:
    """collect_replay_state still works exactly as before."""

    async def test_state_aggregation_unchanged(self) -> None:
        results = [
            _result(event_id="a", stage="store", status="passed"),
            _result(event_id="b", stage="store", status="skipped"),
            _result(event_id="c", stage="store", status="failed", error="bad"),
            _result(event_id="d", stage="store", status="error", error="boom"),
        ]
        state = await collect_replay_state(_aiter(results))

        assert state.events_processed == 4
        assert state.events_passed == 1
        assert state.events_skipped == 1
        assert state.events_failed == 2
        assert state.errors == ["bad", "boom"]

    def test_replay_state_record_unchanged(self) -> None:
        state = ReplayState()
        state.record(ReplayResult(event_id="a", stage="store", status="passed"))
        state.record(ReplayResult(event_id="b", stage="store", status="skipped"))
        state.record(
            ReplayResult(event_id="c", stage="store", status="failed", error="x")
        )
        assert state.events_processed == 3
        assert state.events_passed == 1
        assert state.events_skipped == 1
        assert state.events_failed == 1


# ---------------------------------------------------------------------------
# Integration: summary from ReplayEngine
# ---------------------------------------------------------------------------


class TestReplaySummaryIntegration:
    """ReplaySummary works end-to-end with ReplayEngine."""

    async def test_strict_mode_summary(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        from typing import cast

        from medre.core.storage import ReplayEngine, ReplayRequest

        await temp_storage.append(sample_event)

        engine = ReplayEngine(storage=cast(StorageBackend, temp_storage))
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        summary = await collect_replay_summary(
            engine.replay(request),
            mode=ReplayMode.STRICT,
        )

        assert summary.events_replayed == 1
        assert summary.events_scanned == 1
        assert summary.failure_count == 0
        assert summary.by_status["passed"] == 1
        assert summary.by_stage == {"store": 1}
        assert summary.mode == ReplayMode.STRICT

        # JSON-serialisable
        d = summary.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        assert isinstance(json_str, str)

    async def test_empty_replay_summary(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        from typing import cast

        from medre.core.storage import ReplayEngine, ReplayRequest

        engine = ReplayEngine(storage=cast(StorageBackend, temp_storage))
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        summary = await collect_replay_summary(
            engine.replay(request),
            mode=ReplayMode.STRICT,
        )

        assert summary.events_replayed == 0
        assert summary.events_scanned == 0
        assert summary.failure_count == 0
        assert summary.skipped_count == 0
        assert summary.by_status == {"passed": 0, "skipped": 0, "failed": 0, "error": 0}
        assert summary.by_stage == {}

    async def test_partial_failure_summary(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        from datetime import datetime, timezone
        from typing import cast

        from medre.core.storage import ReplayEngine, ReplayRequest

        await temp_storage.append(sample_event)

        # Add an unregistered-kind event
        bad_event = CanonicalEvent(
            event_id="bad-001",
            event_kind="unknown.event_type",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
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

        engine = ReplayEngine(storage=cast(StorageBackend, temp_storage))
        request = ReplayRequest(mode=ReplayMode.STRICT)

        summary = await collect_replay_summary(
            engine.replay(request),
            events_scanned=2,
            mode=ReplayMode.STRICT,
        )

        assert summary.events_scanned == 2
        assert summary.events_replayed == 2
        assert summary.by_status["passed"] == 1
        assert summary.by_status["failed"] == 1
        assert summary.failure_count == 1
        assert len(summary.errors) == 1

    async def test_dry_run_summary_with_route_resolution(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        from typing import cast

        from medre.core.planning import FallbackResolver
        from medre.core.rendering import RenderingPipeline, TextRenderer
        from medre.core.routing import Route, Router, RouteSource, RouteTarget
        from medre.core.storage import ReplayEngine, ReplayRequest

        route = Route(
            id="test-route",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
        )
        router = Router(routes=[route])
        render_pipe = RenderingPipeline()
        render_pipe.register(TextRenderer(), priority=100)

        # Minimal stub pipeline
        class _Stub:
            async def transform_event(self, event):
                return event

            async def render_event(self, event):
                return await render_pipe.render(event, "test_adapter")

            async def route_event(self, event):
                results = []
                for r in router.match(event):
                    targets = router.resolve_targets(event, r)
                    results.append((r, targets))
                return results

            async def plan_delivery(self, event, routes):
                resolver = FallbackResolver()
                plans = []
                for _r, targets in routes:
                    for t in targets:
                        plans.append(resolver.resolve_fallback(event, t, {}))
                return plans

            async def deliver(self, event, plans):
                return plans

        await temp_storage.append(sample_event)
        engine = ReplayEngine(
            storage=cast(StorageBackend, temp_storage),
            pipeline=_Stub(),
        )
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.DRY_RUN,
        )

        summary = await collect_replay_summary(
            engine.replay(request),
            mode=ReplayMode.DRY_RUN,
        )

        # DRY_RUN: store + route + plan + render + deliver (skipped)
        assert summary.events_replayed == 5
        assert summary.by_status["passed"] == 4  # store, route, plan, render
        assert summary.by_status["skipped"] == 1  # deliver
        assert summary.skipped_count == 1
        assert summary.failure_count == 0
        assert summary.route_resolution_count == 1  # route passed with output
        assert summary.by_stage == {
            "store": 1,
            "route": 1,
            "plan": 1,
            "render": 1,
            "deliver": 1,
        }

        # JSON round-trip
        d = summary.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["by_status"]["skipped"] == 1
        assert parsed["route_resolution_count"] == 1
        assert parsed["mode"] == "dry_run"
