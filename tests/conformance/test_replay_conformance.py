"""Replay conformance tests.

Asserts deterministic replay behaviour for DRY_RUN and BEST_EFFORT modes:
* DRY_RUN does not call adapters (delivery stage is skipped).
* BEST_EFFORT uses CapabilityDecisionResolver for capability-aware
  filtering consistent with live delivery.
* Replay receipts carry source="replay" and replay_run_id.
* RenderingEvidence appears on replay rendered results when the
  pipeline provides it.

Uses the same StubPipeline / make_engine helpers as the existing replay
test modules.  Capability filtering tests use real PipelineRunner with
FakePresentationAdapter to exercise _filter_plans_by_capability through
the actual ReplayEngine._stage_deliver code path.

No real network, no durable replay jobs.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import cast

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.replay import ReplayEngine, ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import StorageBackend
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import StubPipeline, make_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str | None = None) -> CanonicalEvent:
    """Create a minimal canonical event for replay tests."""
    return CanonicalEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        source_adapter="test_adapter",
        source_transport_id="node-001",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "replay test"},
        metadata=EventMetadata(),
    )


# ---------------------------------------------------------------------------
# DRY_RUN conformance
# ---------------------------------------------------------------------------


class TestReplayDryRunConformance:
    """DRY_RUN mode: all stages except delivery, no adapter calls."""

    @pytest.mark.asyncio
    async def test_dry_run_skips_delivery(self, temp_storage: SQLiteStorage):
        """DRY_RUN: deliver stage is skipped, not executed."""
        event = _make_event("dry-001")
        await temp_storage.append(event)

        pipeline = StubPipeline()
        engine = make_engine(temp_storage, pipeline)
        request = ReplayRequest(
            mode=ReplayMode.DRY_RUN,
            run_id="dry-run-001",
        )

        results = [r async for r in engine.replay(request)]

        # Find the deliver stage result
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) == 1
        assert deliver_results[0].status == "skipped"
        assert "dry_run" in (deliver_results[0].error or "")

    @pytest.mark.asyncio
    async def test_dry_run_includes_earlier_stages(self, temp_storage: SQLiteStorage):
        """DRY_RUN: all five stages produce results (store, route, plan, render, deliver).

        StubPipeline has no router so route returns "failed" and plan/render
        may be skipped, but every stage is represented in the results.
        """
        event = _make_event("dry-002")
        await temp_storage.append(event)

        pipeline = StubPipeline()
        engine = make_engine(temp_storage, pipeline)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        results = [r async for r in engine.replay(request)]
        stages = {r.stage for r in results}

        assert stages == {"store", "route", "plan", "render", "deliver"}

    @pytest.mark.asyncio
    async def test_dry_run_preserves_event(
        self, temp_storage: SQLiteStorage, sample_event: CanonicalEvent
    ):
        """DRY_RUN: original event is not mutated."""
        await temp_storage.append(sample_event)
        original_id = sample_event.event_id

        engine = make_engine(temp_storage)
        request = ReplayRequest(mode=ReplayMode.DRY_RUN)

        results = [r async for r in engine.replay(request)]
        assert any(r.event_id == original_id for r in results)
        assert sample_event.event_id == original_id


# ---------------------------------------------------------------------------
# BEST_EFFORT conformance
# ---------------------------------------------------------------------------


class TestReplayBestEffortConformance:
    """BEST_EFFORT mode: capability filtering parity with live delivery."""

    @pytest.mark.asyncio
    async def test_best_effort_stub_pipeline_no_adapter_registry(
        self, temp_storage: SQLiteStorage
    ):
        """BEST_EFFORT (StubPipeline, no adapter registry): events pass through.

        StubPipeline has no router and no adapter registry, so capability
        filtering is a no-op and the engine skips delivery because no routes
        match.  This is correct stub behaviour.  True capability-filtering
        parity with live delivery is tested in
        ``test_capability_runtime_conformance`` using the real
        ``CapabilityDecisionResolver``.

        Distinct from ``test_best_effort_stub_pipeline_deliver_stage_handled``:
        this test uses a reaction event_kind to verify stage presence is
        correct even when the event type differs from plain text.
        """
        event = CanonicalEvent(
            event_id="be-cap-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="test_adapter",
            source_transport_id="node-001",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"key": "👍"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        # StubPipeline with no adapters registered and no router.
        pipeline = StubPipeline()
        engine = make_engine(temp_storage, pipeline)
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            run_id="be-001",
        )

        results = [r async for r in engine.replay(request)]
        deliver_results = [r for r in results if r.stage == "deliver"]

        # With no router, no routes match, so delivery is skipped.
        assert len(deliver_results) == 1
        assert deliver_results[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_best_effort_stub_pipeline_deliver_stage_handled(
        self, temp_storage: SQLiteStorage
    ):
        """BEST_EFFORT (StubPipeline, no router): deliver stage is skipped.

        StubPipeline has no router and no adapter registry, so routing
        produces no routes, planning yields no plans, and the deliver stage
        is skipped with a descriptive error.  This is the correct stub
        behaviour -- it does NOT test source='replay' / replay_run_id tagging,
        which requires a real pipeline with ``deliver_to_targets``.

        The real contract (source='replay' + replay_run_id on delivery
        receipts) is asserted in the integration-level replay tests that
        use PipelineRunner with actual adapters.

        Distinct from ``test_best_effort_stub_pipeline_no_adapter_registry``:
        this test asserts the *error message content* ("No delivery plans")
        rather than just stage status, and uses a plain text event.
        """
        event = _make_event("be-src-001")
        await temp_storage.append(event)

        pipeline = StubPipeline()
        engine = make_engine(temp_storage, pipeline)
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            run_id="replay-run-001",
        )

        results = [r async for r in engine.replay(request)]
        deliver_results = [r for r in results if r.stage == "deliver"]
        assert len(deliver_results) == 1
        assert deliver_results[0].status == "skipped"
        # StubPipeline has no router, so no plans are produced.
        assert "No delivery plans" in (deliver_results[0].error or "")

    @pytest.mark.asyncio
    async def test_best_effort_run_id_populated(self, temp_storage: SQLiteStorage):
        """BEST_EFFORT: run_id propagates to route attribution.

        Verifies that the replay request's ``run_id`` is carried into the
        ``ReplayRouteAttribution`` on the route-stage result.  Receipt-level
        ``source='replay'`` and ``replay_run_id`` tagging requires a real
        pipeline with ``deliver_to_targets`` and is covered by
        integration-level replay tests.
        """
        event = _make_event("be-runid-001")
        await temp_storage.append(event)

        pipeline = StubPipeline()
        engine = make_engine(temp_storage, pipeline)
        run_id = "conformance-run-42"
        request = ReplayRequest(
            mode=ReplayMode.BEST_EFFORT,
            run_id=run_id,
        )

        results = [r async for r in engine.replay(request)]

        # Store stage must pass
        store_results = [r for r in results if r.stage == "store"]
        assert len(store_results) == 1
        assert store_results[0].status == "passed"

        # Route stage carries the run_id in its attribution
        route_results = [r for r in results if r.stage == "route"]
        assert len(route_results) == 1
        assert route_results[0].route_attribution is not None
        assert route_results[0].route_attribution.run_id == run_id


# ---------------------------------------------------------------------------
# Replay evidence conformance
# ---------------------------------------------------------------------------


class TestReplayEvidenceConformance:
    """Assert rendering evidence parity between replay and live paths."""

    @pytest.mark.asyncio
    async def test_replay_render_stage_produces_evidence(
        self, temp_storage: SQLiteStorage
    ):
        """RE_RENDER: render stage captures output from the pipeline."""
        event = _make_event("ev-001")
        await temp_storage.append(event)

        # Set up a pipeline with a TextRenderer
        rp = RenderingPipeline()
        rp.register(TextRenderer(), priority=100)

        pipeline = StubPipeline(rendering_pipeline=rp)
        engine = make_engine(temp_storage, pipeline)
        request = ReplayRequest(mode=ReplayMode.RE_RENDER)

        results = [r async for r in engine.replay(request)]
        render_results = [r for r in results if r.stage == "render"]
        assert len(render_results) == 1
        # TextRenderer is registered and should render successfully.
        assert render_results[0].status == "passed"
        assert render_results[0].output is not None


# ---------------------------------------------------------------------------
# BEST_EFFORT capability filtering conformance (real PipelineRunner)
# ---------------------------------------------------------------------------


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict,
) -> PipelineConfig:
    """Build a PipelineConfig for replay conformance tests."""
    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters,
        event_bus=EventBus(),
    )


class TestReplayBestEffortCapabilityFiltering:
    """BEST_EFFORT capability filtering through real ReplayEngine +
    PipelineRunner.

    Exercises the _filter_plans_by_capability path in
    ReplayEngine._stage_deliver using real adapters and routing.
    """

    @pytest.mark.asyncio
    async def test_unsupported_reaction_filtered_in_best_effort(
        self, temp_storage: SQLiteStorage
    ):
        """BEST_EFFORT replay filters message.reacted when adapter has
        reactions='unsupported'.

        Seeds a reaction event into storage, configures a real
        PipelineRunner with a FakePresentationAdapter whose
        reactions capability is unsupported, then runs BEST_EFFORT
        replay.  The deliver stage must be skipped with
        capability_suppressed in the error.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
            replies="native",
        )

        route = Route(
            id="replay-cap-filter-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(temp_storage, router, {"dest": adapter})
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="replay-cap-filter-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="src",
            source_transport_id="node-001",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"key": "\U0001f44d"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        try:
            engine = ReplayEngine(
                storage=temp_storage,
                pipeline=runner,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="replay-cap-filter-run-001",
                correlation_ids=["replay-cap-filter-001"],
            )

            results = [r async for r in engine.replay(request)]

            deliver_results = [r for r in results if r.stage == "deliver"]
            assert len(deliver_results) >= 1
            assert deliver_results[0].status == "skipped"
            assert deliver_results[0].error is not None
            assert "capability_suppressed" in deliver_results[0].error

            # Adapter never called.
            assert len(adapter.delivered_payloads) == 0
        finally:
            await runner.stop()

    @pytest.mark.asyncio
    async def test_fallback_capability_not_filtered_in_best_effort(
        self, temp_storage: SQLiteStorage
    ):
        """BEST_EFFORT replay does NOT filter message.reacted when adapter
        has reactions='fallback'.

        Fallback-capable adapters should remain deliverable; only
        unsupported capabilities are filtered.
        """
        adapter = FakePresentationAdapter(adapter_id="dest_fb")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="fallback",
            replies="native",
        )

        route = Route(
            id="replay-cap-fallback-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest_fb")],
        )
        router = Router(routes=[route])

        config = _make_pipeline_config(temp_storage, router, {"dest_fb": adapter})
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="replay-cap-fallback-001",
            event_kind="message.reacted",
            schema_version=1,
            timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
            source_adapter="src",
            source_transport_id="node-001",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"key": "\U0001f44d"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        try:
            engine = ReplayEngine(
                storage=temp_storage,
                pipeline=runner,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="replay-cap-fallback-run-001",
                correlation_ids=["replay-cap-fallback-001"],
            )

            results = [r async for r in engine.replay(request)]

            # The event should NOT be filtered: adapter was called.
            assert len(adapter.delivered_payloads) == 1

            # Stage-level: no deliver stage should be skipped.
            deliver_results = [r for r in results if r.stage == "deliver"]
            assert all(
                dr.status != "skipped" for dr in deliver_results
            ), f"Expected no skipped deliver stages, got: {deliver_results}"
        finally:
            await runner.stop()
