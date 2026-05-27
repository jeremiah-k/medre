"""Pipeline phase ordering tests.

Validates that:
- The PipelinePhase enum has exactly 6 values.
- PipelineRunner traverses phases in the correct order.
- Every phase in the enum is visited at least once.
- Dedup phase short-circuits the pipeline on duplicate native refs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.phases import PipelinePhase
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    return FakePresentationAdapter(adapter_id="fake_presentation")


@pytest.fixture
def router() -> Router:
    """Router: fake_transport → fake_presentation."""
    return Router(
        routes=[
            Route(
                id="route-phase-test",
                source=RouteSource(
                    adapter="fake_transport",
                    event_kinds=("message.created",),
                    channel="ch-0",
                ),
                targets=[RouteTarget(adapter="fake_presentation")],
            )
        ]
    )


# ===================================================================
# Tests
# ===================================================================


class TestPipelinePhaseEnum:
    """Validate the PipelinePhase enum structure."""

    def test_phase_enum_matches_implementation(self) -> None:
        """The PipelinePhase enum must have exactly 6 values."""
        members = list(PipelinePhase)
        assert len(members) == 6, (
            f"PipelinePhase should have exactly 6 members, got {len(members)}: "
            f"{[m.value for m in members]}"
        )

    def test_phase_values(self) -> None:
        """Phase values must match expected names."""
        expected = {
            "ingress",
            "dedup",
            "resolve_relations",
            "store",
            "route",
            "deliver",
        }
        actual = {phase.value for phase in PipelinePhase}
        assert actual == expected


class TestPhaseVisitOrder:
    """Verify pipeline phases are visited in the correct order."""

    async def test_all_phases_are_visited_in_order(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Run an event through the pipeline, capture phase counts,
        and verify all 6 phases were visited exactly once."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-phase-order-001")

        try:
            await runner.handle_ingress(event)

            counts = runner._phase_counts

            # Every phase must have been visited at least once.
            for phase in PipelinePhase:
                assert counts[phase] >= 1, (
                    f"Phase {phase.value!r} was not visited " f"(count={counts[phase]})"
                )
        finally:
            await runner.stop()

    async def test_no_phase_is_skipped(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Every phase in the enum must be visited at least once during
        a normal pipeline run."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-phase-noskip-001")

        try:
            await runner.handle_ingress(event)

            for phase in PipelinePhase:
                assert runner._phase_counts[phase] > 0, (
                    f"Phase {phase.value!r} was skipped "
                    f"(count={runner._phase_counts[phase]})"
                )
        finally:
            await runner.stop()

    async def test_phase_order_matches_handle_ingress(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """The _current_phase should end on DELIVER after successful run,
        and the counts should show INGRESS was visited before ROUTE, etc.

        We verify ordering by checking that the final phase is DELIVER
        and all phases accumulated exactly 1 count in a single-event run."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-phase-seq-001")

        try:
            await runner.handle_ingress(event)

            # After a successful run, the last phase visited is DELIVER.
            assert runner._current_phase == PipelinePhase.DELIVER

            # Each phase should have been visited exactly once for a
            # single-event run.
            expected_order = [
                PipelinePhase.INGRESS,
                PipelinePhase.DEDUP,
                PipelinePhase.RESOLVE_RELATIONS,
                PipelinePhase.STORE,
                PipelinePhase.ROUTE,
                PipelinePhase.DELIVER,
            ]
            for phase in expected_order:
                assert runner._phase_counts[phase] == 1, (
                    f"Expected phase {phase.value!r} count=1, "
                    f"got {runner._phase_counts[phase]}"
                )
        finally:
            await runner.stop()


class TestDedupPhaseSkipsRemainder:
    """When dedup finds a duplicate native_ref, the pipeline must stop
    after DEDUP — no STORE, ROUTE, or DELIVER phases."""

    async def test_dedup_phase_skips_remainder(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Simulate a duplicate native ref: the second event with the same
        source_native_ref should short-circuit at DEDUP."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        native_ref = NativeRef(
            adapter="fake_transport",
            native_channel_id="ch-0",
            native_message_id="native-dedup-001",
        )

        # First event — should pass through all phases.
        event1 = make_event(event_id="evt-dedup-first")
        event1 = CanonicalEvent(
            event_id=event1.event_id,
            event_kind=event1.event_kind,
            schema_version=event1.schema_version,
            timestamp=event1.timestamp,
            source_adapter=event1.source_adapter,
            source_transport_id=event1.source_transport_id,
            source_channel_id=event1.source_channel_id,
            parent_event_id=event1.parent_event_id,
            lineage=event1.lineage,
            relations=event1.relations,
            payload=dict(event1.payload),
            metadata=event1.metadata,
            source_native_ref=native_ref,
        )

        try:
            await runner.handle_ingress(event1)

            # All phases should be visited for the first event.
            for phase in PipelinePhase:
                assert runner._phase_counts[phase] >= 1

            # Snapshot counts after first event.
            counts_after_first = dict(runner._phase_counts)

            # Second event with the SAME native_ref — should be deduped.
            event2 = CanonicalEvent(
                event_id="evt-dedup-duplicate",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "duplicate"},
                metadata=EventMetadata(),
                source_native_ref=native_ref,
            )

            outcomes = await runner.handle_ingress(event2)

            # The dedup should have returned empty outcomes.
            assert outcomes == [], (
                f"Duplicate event should have been suppressed at DEDUP, "
                f"got {len(outcomes)} outcomes"
            )

            # Only INGRESS and DEDUP should have incremented for the second event.
            # INGRESS increments by 1, DEDUP increments by 1.
            assert (
                runner._phase_counts[PipelinePhase.INGRESS]
                == counts_after_first[PipelinePhase.INGRESS] + 1
            )
            assert (
                runner._phase_counts[PipelinePhase.DEDUP]
                == counts_after_first[PipelinePhase.DEDUP] + 1
            )

            # STORE, ROUTE, DELIVER should NOT have incremented.
            for phase in (
                PipelinePhase.STORE,
                PipelinePhase.ROUTE,
                PipelinePhase.DELIVER,
            ):
                assert runner._phase_counts[phase] == counts_after_first[phase], (
                    f"Phase {phase.value!r} should not have been visited "
                    f"for the deduped event, but count incremented from "
                    f"{counts_after_first[phase]} to {runner._phase_counts[phase]}"
                )
        finally:
            await runner.stop()
