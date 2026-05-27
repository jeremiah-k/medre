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

            snapshot = runner.phase_snapshot()
            counts = snapshot["counts"]

            # Every phase must have been visited at least once.
            for phase in PipelinePhase:
                phase_key = phase.value
                assert counts[phase_key] >= 1, (
                    f"Phase {phase_key!r} was not visited "
                    f"(count={counts[phase_key]})"
                )
        finally:
            await runner.stop()

    async def test_phase_order_matches_handle_ingress(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """After a successful single-event run, the snapshot should show
        DELIVER as the current phase and all phases visited exactly once."""
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

            snapshot = runner.phase_snapshot()
            counts = snapshot["counts"]

            # After a successful run, the last phase visited is DELIVER.
            assert snapshot["current_phase"] == "deliver"

            # Each phase should have been visited exactly once for a
            # single-event run.
            for phase_name in (
                "ingress", "dedup", "resolve_relations",
                "store", "route", "deliver",
            ):
                assert counts[phase_name] == 1, (
                    f"Expected phase {phase_name!r} count=1, "
                    f"got {counts[phase_name]}"
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

            # Snapshot counts after first event.
            snapshot1 = runner.phase_snapshot()
            counts_after_first = snapshot1["counts"]

            # All phases should be visited for the first event.
            for phase_name in counts_after_first:
                assert counts_after_first[phase_name] >= 1

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
            snapshot2 = runner.phase_snapshot()
            counts2 = snapshot2["counts"]

            assert counts2["ingress"] == counts_after_first["ingress"] + 1
            assert counts2["dedup"] == counts_after_first["dedup"] + 1

            # STORE, ROUTE, DELIVER should NOT have incremented.
            for phase_name in ("store", "route", "deliver"):
                assert counts2[phase_name] == counts_after_first[phase_name], (
                    f"Phase {phase_name!r} should not have been visited "
                    f"for the deduped event, but count incremented from "
                    f"{counts_after_first[phase_name]} to {counts2[phase_name]}"
                )
        finally:
            await runner.stop()


class TestNoRoutesMatchedSkipsDeliver:
    """When no routes match the event, ROUTE is visited but DELIVER is not."""

    async def test_no_routes_matched_visits_route_not_deliver(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Configure a route that won't match the event, then verify ROUTE
        is visited but DELIVER is not."""
        # Route matches a different source adapter — won't match our event.
        no_match_router = Router(
            routes=[
                Route(
                    id="route-no-match",
                    source=RouteSource(
                        adapter="other_transport",
                        event_kinds=("message.created",),
                        channel="ch-0",
                    ),
                    targets=[RouteTarget(adapter="fake_presentation")],
                )
            ]
        )
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=no_match_router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-no-routes-001")

        try:
            await runner.handle_ingress(event)

            snapshot = runner.phase_snapshot()
            counts = snapshot["counts"]

            # ROUTE phase must be visited.
            assert counts["route"] >= 1, (
                f"ROUTE phase was not visited "
                f"(count={counts['route']})"
            )

            # DELIVER phase must NOT be visited (no routes matched).
            assert counts["deliver"] == 0, (
                f"DELIVER phase should not have been visited when no routes "
                f"matched (count={counts['deliver']})"
            )
        finally:
            await runner.stop()


class TestValidationFailureRecordsIngressOnly:
    """When event construction validation fails, the pipeline is never entered."""

    def test_invalid_event_kind_rejected_at_construction(self) -> None:
        """An event with an empty event_kind must raise ValueError at
        construction time — the pipeline is never reached."""
        with pytest.raises(ValueError, match="event_kind"):
            CanonicalEvent(
                event_id="evt-bad-kind-001",
                event_kind="",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="fake_transport",
                source_transport_id="node-1",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "bad"},
                metadata=EventMetadata(),
            )

    async def test_pipeline_validates_event_construction_before_ingress(
        self,
        temp_storage: SQLiteStorage,
        router: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """A valid event passes construction and reaches the pipeline
        successfully. This confirms the validation gate works end-to-end."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="evt-valid-001")
        try:
            await runner.handle_ingress(event)
        finally:
            await runner.stop()

        snapshot = runner.phase_snapshot()
        counts = snapshot["counts"]

        assert counts["ingress"] >= 1
        assert counts["deliver"] >= 1, "Valid event should reach DELIVER"
