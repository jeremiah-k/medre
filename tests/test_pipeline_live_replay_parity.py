"""Live vs replay parity tests.

Proves the pipeline delivers semantically equivalent plans and receipts
through both the live path (PipelineRunner) and the replay path
(ReplayEngine).  Comparison normalises away time-based fields (deadline,
duration_ms) and identity fields that differ by design (receipt_id,
parent_receipt_id, source, replay_run_id, created_at, adapter_message_id).

Each test seeds an event into storage, runs it through the live pipeline,
then runs the same event through the replay engine, and compares normalised
DeliveryPlan and DeliveryReceipt fields.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.protocols import _RealPipelineProtocol
from medre.core.engine.replay.types import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent
from medre.core.planning.delivery_plan import DeliveryOutcome, DeliveryPlan
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalize_plan(plan: DeliveryPlan) -> dict[str, Any]:
    """Extract semantically comparable fields from a DeliveryPlan.

    Skips time-based fields (deadline) and duration fields because live and
    replay runs execute at different wall-clock times.  The plan_id is
    deterministic (derived from event_id + target + route_id + target_index)
    so it IS comparable across paths.
    """
    return {
        "plan_id": plan.plan_id,
        "event_id": plan.event_id,
        "target_adapter": plan.target.adapter,
        "target_channel": plan.target.channel,
        "primary_strategy_method": plan.primary_strategy.method,
        "route_id": plan.route_id,
        "target_identity": plan.target_identity,
        "capability_level": plan.capability_level,
        "capability_field": plan.capability_field,
        "capability_reason": plan.capability_reason,
    }


def normalize_receipt(receipt: Any) -> dict[str, Any]:
    """Extract semantically comparable fields from a DeliveryReceipt.

    Skips identity fields that differ by design:
    - receipt_id (UUID, different each run)
    - parent_receipt_id (may differ)
    - source ("live" vs "replay")
    - replay_run_id (only set on replay)
    - created_at (wall-clock timestamp)
    - adapter_message_id (generated per delivery)
    - attempt_number (replay is the next attempt, not the same one)

    Skips timing fields:
    - next_retry_at (wall-clock)
    """
    return {
        "event_id": receipt.event_id,
        "delivery_plan_id": receipt.delivery_plan_id,
        "target_adapter": receipt.target_adapter,
        "target_channel": receipt.target_channel,
        "route_id": receipt.route_id,
        "status": receipt.status,
        "error": receipt.error,
        "failure_kind": receipt.failure_kind,
        "rendering_evidence": receipt.rendering_evidence,
    }


# ---------------------------------------------------------------------------
# Shared async fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def parity_env(temp_storage: SQLiteStorage):
    """Set up a shared environment for live + replay parity tests.

    Provides:
        storage      – SQLiteStorage (from root conftest temp_storage)
        adapter      – FakeTransportAdapter registered as "dest"
        runner       – PipelineRunner (live path)
        replay       – ReplayEngine (replay path)
        router       – Router with sample routes
        seed_event   – helper to store an event for replay
    """

    env = SimpleNamespace()
    env.storage = temp_storage
    env.adapter = FakeTransportAdapter(adapter_id="dest", channel="ch-0")

    # Build a default route: src -> dest for message.created events.
    route = Route(
        id="parity-route",
        source=RouteSource(
            adapter="src",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter="dest")],
    )
    router = Router(routes=[route])
    env.router = router
    env.route = route

    config = make_pipeline_config_for_pipeline(
        storage=temp_storage,
        router=router,
        adapters={"dest": env.adapter},
    )
    runner = PipelineRunner(config)
    await runner.start()
    env.runner = runner
    env.config = config

    replay = ReplayEngine(
        storage=temp_storage,
        pipeline=cast(_RealPipelineProtocol, runner),
    )
    env.replay = replay

    async def seed_event(event: CanonicalEvent) -> CanonicalEvent:
        """Store event so replay engine can find it."""
        await temp_storage.append(event)
        return event

    env.seed_event = seed_event

    yield env

    await runner.stop()


# ---------------------------------------------------------------------------
# Helper: collect replay DeliveryOutcomes for an event
# ---------------------------------------------------------------------------


async def _collect_replay_outcomes(
    replay: ReplayEngine, event_id: str
) -> list[DeliveryOutcome]:
    """Run BEST_EFFORT replay and collect all DeliveryOutcome objects."""
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        correlation_ids=[event_id],
    )
    outcomes: list[DeliveryOutcome] = []
    async for result in replay.replay(request):
        if result.stage == "deliver" and result.status == "passed":
            envelope = result.output
            if isinstance(envelope, dict):
                adapter_results = envelope.get("adapter_results", [])
                for outcome in adapter_results:
                    if isinstance(outcome, DeliveryOutcome):
                        outcomes.append(outcome)
    return outcomes


# ---------------------------------------------------------------------------
# Helper: run event through live pipeline
# ---------------------------------------------------------------------------


async def _run_live(
    runner: PipelineRunner, event: CanonicalEvent
) -> list[DeliveryOutcome]:
    """Run event through live pipeline and return outcomes."""
    return await runner.handle_ingress(event)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDirectDeliveryPlanParity:
    """Direct delivery: live and replay produce equivalent plans."""

    async def test_direct_delivery_plan_parity(self, parity_env) -> None:
        """Compare plan fields for a direct (normal) delivery.

        Live path produces DeliveryPlan via PipelineRunner.route_event;
        replay path produces DeliveryPlan via ReplayEngine RE_ROUTE mode.
        The plans should have identical plan_id, route_id, target_identity,
        capability metadata, and strategy method.
        """
        env = parity_env

        # -- Live path: route the event to get the plan --
        live_event = make_event(
            event_id="parity-direct-001",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "direct delivery parity"},
        )
        _, live_deliveries = await env.runner.route_event(live_event)
        assert len(live_deliveries) >= 1, "Live path should produce at least one plan"
        _live_route, live_plan = live_deliveries[0]
        live_norm = normalize_plan(live_plan)

        # -- Store the event for replay --
        await env.seed_event(live_event)

        # -- Replay path: RE_ROUTE produces plans via route stage output --
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-direct-001"],
        )
        replay_plans: list[DeliveryPlan] = []
        async for result in env.replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                # route output is list[tuple[Route, DeliveryPlan]]
                for _r, plan in result.output:
                    replay_plans.append(plan)

        assert (
            len(replay_plans) >= 1
        ), f"Replay path should produce at least one plan, got {len(replay_plans)}"
        replay_norm = normalize_plan(replay_plans[0])

        # -- Pre-comparison: metadata must be populated --
        assert live_plan.target_identity, "live target_identity must be non-empty"
        assert replay_plans[
            0
        ].target_identity, "replay target_identity must be non-empty"
        assert (
            live_plan.capability_field is not None
        ), "live capability_field must be populated"
        assert (
            replay_plans[0].capability_field is not None
        ), "replay capability_field must be populated"

        # -- Compare normalised plans --
        assert live_norm == replay_norm, (
            f"Live plan != replay plan.\n"
            f"  Live:   {live_norm}\n"
            f"  Replay: {replay_norm}"
        )


class TestFallbackTextDeliveryPlanParity:
    """Fallback_text delivery path parity."""

    async def test_fallback_text_delivery_plan_parity(self, parity_env) -> None:
        """Compare plan fields when the adapter degrades to fallback_text.

        Uses an adapter object exposing _capabilities with
        reactions='fallback' so that a reaction event produces a
        fallback_text strategy.  Both paths should agree on the degraded
        strategy.
        """
        env = parity_env

        class _FallbackReactions:
            """Adapter that exposes reactions='fallback' via _capabilities."""

            adapter_id = "fallback_dest"
            _capabilities = AdapterCapabilities(reactions="fallback")

        fallback_adapter = _FallbackReactions()
        route = Route(
            id="fallback-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fallback_dest")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=env.storage,
            router=router,
            adapters={"fallback_dest": fallback_adapter},
        )
        runner = PipelineRunner(config)

        # Live path
        live_event = make_event(
            event_id="parity-fallback-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "👍"},
        )
        _, live_deliveries = await runner.route_event(live_event)
        assert len(live_deliveries) >= 1
        _lr, live_plan = live_deliveries[0]
        live_norm = normalize_plan(live_plan)

        # Store for replay
        await env.storage.append(live_event)

        # Replay path
        replay = ReplayEngine(
            storage=env.storage, pipeline=cast(_RealPipelineProtocol, runner)
        )
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-fallback-001"],
        )
        replay_plans: list[DeliveryPlan] = []
        async for result in replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    replay_plans.append(plan)

        assert len(replay_plans) >= 1
        replay_norm = normalize_plan(replay_plans[0])

        assert live_norm == replay_norm, (
            f"Fallback plan mismatch.\n"
            f"  Live:   {live_norm}\n"
            f"  Replay: {replay_norm}"
        )

        # Both should use fallback_text (not "skip") because the
        # adapter declares reactions="fallback".
        assert live_norm["primary_strategy_method"] == "fallback_text", (
            f"Expected fallback_text strategy, got "
            f"{live_norm['primary_strategy_method']}"
        )


class TestCapabilitySkipParity:
    """Capability skip/suppression parity."""

    async def test_capability_skip_parity(self, parity_env) -> None:
        """Both paths produce skip strategy when capability is unsupported.

        An adapter with reactions="unsupported" should cause a reaction
        event to produce a "skip" delivery strategy in both live and replay.
        """
        env = parity_env

        class _NoReactions:
            """Adapter that does not support reactions."""

            _capabilities = AdapterCapabilities(reactions="unsupported")

        route = Route(
            id="skip-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="no_react")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=env.storage,
            router=router,
            adapters={"no_react": _NoReactions()},
        )
        runner = PipelineRunner(config)

        # Live path
        live_event = make_event(
            event_id="parity-skip-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"key": "+1"},
        )
        _, live_deliveries = await runner.route_event(live_event)
        assert len(live_deliveries) >= 1
        _lr, live_plan = live_deliveries[0]
        assert live_plan.target_identity, "target_identity must be populated"
        assert live_plan.capability_level, "capability_level must be populated"
        assert live_plan.capability_field, "capability_field must be populated"
        live_norm = normalize_plan(live_plan)

        assert live_norm["primary_strategy_method"] == "skip", (
            f"Live path should produce 'skip' strategy, "
            f"got {live_norm['primary_strategy_method']}"
        )

        # Store for replay
        await env.storage.append(live_event)

        # Replay path
        replay = ReplayEngine(
            storage=env.storage, pipeline=cast(_RealPipelineProtocol, runner)
        )
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-skip-001"],
        )
        replay_plans: list[DeliveryPlan] = []
        async for result in replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    replay_plans.append(plan)

        assert len(replay_plans) >= 1
        replay_norm = normalize_plan(replay_plans[0])

        assert replay_norm["primary_strategy_method"] == "skip", (
            f"Replay path should produce 'skip' strategy, "
            f"got {replay_norm['primary_strategy_method']}"
        )

        assert (
            live_norm == replay_norm
        ), f"Skip plan mismatch.\n  Live: {live_norm}\n  Replay: {replay_norm}"


class TestMissingAdapterParity:
    """Missing/unknown adapter path parity."""

    async def test_missing_adapter_parity(self, parity_env) -> None:
        """Both paths handle missing adapter consistently.

        When the route targets an adapter that is not registered in the
        pipeline config, both paths should still produce a plan (the plan
        is created during routing, before adapter lookup).  The plan fields
        should be equivalent.
        """
        env = parity_env

        route = Route(
            id="missing-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="nonexistent_adapter")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=env.storage,
            router=router,
            adapters={},  # No adapters registered.
        )
        runner = PipelineRunner(config)

        # Live path — route_event still produces a plan.
        live_event = make_event(
            event_id="parity-missing-001",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "missing adapter"},
        )
        _, live_deliveries = await runner.route_event(live_event)
        assert (
            len(live_deliveries) >= 1
        ), "Live path should still plan delivery even for missing adapter"
        _lr, live_plan = live_deliveries[0]
        live_norm = normalize_plan(live_plan)

        # Store for replay
        await env.storage.append(live_event)

        # Replay path
        replay = ReplayEngine(
            storage=env.storage, pipeline=cast(_RealPipelineProtocol, runner)
        )
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-missing-001"],
        )
        replay_plans: list[DeliveryPlan] = []
        async for result in replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    replay_plans.append(plan)

        assert len(replay_plans) >= 1
        replay_norm = normalize_plan(replay_plans[0])

        assert live_norm == replay_norm, (
            f"Missing-adapter plan mismatch.\n"
            f"  Live:   {live_norm}\n"
            f"  Replay: {replay_norm}"
        )


class TestRepeatedEquivalentTargetsParity:
    """Repeated equivalent targets get distinct deterministic plan IDs."""

    async def test_repeated_equivalent_targets_parity(self, parity_env) -> None:
        """Both paths assign distinct plan IDs to repeated equivalent targets.

        A route with two identical targets should produce two distinct
        plan IDs (disambiguated by target_index).  Both live and replay
        paths must produce the same set of plan IDs.
        """
        env = parity_env

        target = RouteTarget(adapter="dest", channel="ch-0")
        route = Route(
            id="dup-target-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[target, RouteTarget(adapter="dest", channel="ch-0")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=env.storage,
            router=router,
            adapters={"dest": env.adapter},
        )
        runner = PipelineRunner(config)

        # Live path
        live_event = make_event(
            event_id="parity-dup-001",
            source_adapter="src",
            source_channel_id="ch-0",
        )
        _, live_deliveries = await runner.route_event(live_event)
        assert (
            len(live_deliveries) == 2
        ), f"Expected 2 plans for duplicate targets, got {len(live_deliveries)}"
        live_plan_ids = [plan.plan_id for _, plan in live_deliveries]
        assert (
            len(set(live_plan_ids)) == 2
        ), f"Duplicate targets should have distinct plan IDs, got {live_plan_ids}"

        # Store for replay
        await env.storage.append(live_event)

        # Replay path
        replay = ReplayEngine(
            storage=env.storage, pipeline=cast(_RealPipelineProtocol, runner)
        )
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-dup-001"],
        )
        replay_plan_ids: list[str] = []
        async for result in replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    replay_plan_ids.append(plan.plan_id)

        assert (
            len(replay_plan_ids) == 2
        ), f"Replay should produce 2 plans, got {len(replay_plan_ids)}"
        assert len(set(replay_plan_ids)) == 2, (
            f"Replay duplicate targets should have distinct plan IDs, "
            f"got {replay_plan_ids}"
        )

        # Plan IDs must match exactly between live and replay.
        assert sorted(live_plan_ids) == sorted(replay_plan_ids), (
            f"Live plan IDs {sorted(live_plan_ids)} != "
            f"replay plan IDs {sorted(replay_plan_ids)}"
        )


class TestReplayDeterministicPlanIds:
    """Replay path uses deterministic plan IDs."""

    async def test_replay_with_deterministic_plan_ids(self, parity_env) -> None:
        """Repeated replays of the same event produce identical plan IDs.

        The plan_id is derived from event_id + route_id + target_index +
        target_hash, so two consecutive replays should yield the exact
        same plan IDs.  This proves determinism.
        """
        env = parity_env

        event = make_event(
            event_id="parity-deterministic-001",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "deterministic plans"},
        )
        await env.seed_event(event)

        # First replay
        request = ReplayRequest(
            mode=ReplayMode.RE_ROUTE,
            correlation_ids=["parity-deterministic-001"],
        )
        run1_ids: list[str] = []
        async for result in env.replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    run1_ids.append(plan.plan_id)

        assert len(run1_ids) >= 1, "First replay should produce at least one plan"

        # Second replay (same event, same routes)
        run2_ids: list[str] = []
        async for result in env.replay.replay(request):
            if result.stage == "route" and result.status == "passed":
                for _r, plan in result.output:
                    run2_ids.append(plan.plan_id)

        assert sorted(run1_ids) == sorted(run2_ids), (
            f"Repeated replays should produce identical plan IDs.\n"
            f"  Run 1: {sorted(run1_ids)}\n"
            f"  Run 2: {sorted(run2_ids)}"
        )

        # Also verify live path produces the same plan IDs.
        _, live_deliveries = await env.runner.route_event(event)
        live_ids = [plan.plan_id for _, plan in live_deliveries]
        assert sorted(live_ids) == sorted(run1_ids), (
            f"Live plan IDs should match replay plan IDs.\n"
            f"  Live:   {sorted(live_ids)}\n"
            f"  Replay: {sorted(run1_ids)}"
        )


class TestReceiptParity:
    """Receipt field parity between live and replay paths."""

    async def test_receipt_semantic_parity(self, parity_env) -> None:
        """Live and replay receipts have matching normalised fields.

        After live delivery and replay delivery of the same event,
        the normalised receipt fields (event_id, plan_id, target_adapter,
        status, etc.) should be identical.  Fields that differ by design
        (receipt_id, source, created_at, etc.) are excluded from
        comparison.
        """
        env = parity_env

        event = make_event(
            event_id="parity-receipt-001",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"text": "receipt parity"},
        )

        # -- Live path: deliver and get receipt --
        # handle_ingress stores the event, so we don't need to seed it
        # separately for replay.
        live_outcomes = await _run_live(env.runner, event)
        assert len(live_outcomes) >= 1, "Live delivery should produce outcomes"
        live_outcome = live_outcomes[0]
        assert live_outcome.status == "success", (
            f"Live delivery should succeed, got {live_outcome.status}: "
            f"{live_outcome.error}"
        )
        assert live_outcome.receipt is not None, "Live outcome should have a receipt"
        live_receipt_norm = normalize_receipt(live_outcome.receipt)

        # -- Replay path: BEST_EFFORT to get delivery outcomes --
        # The event is already stored by handle_ingress above.
        replay_outcomes = await _collect_replay_outcomes(
            env.replay, "parity-receipt-001"
        )
        assert (
            len(replay_outcomes) >= 1
        ), f"Replay should produce delivery outcomes, got {len(replay_outcomes)}"
        replay_outcome = replay_outcomes[0]
        assert (
            replay_outcome.receipt is not None
        ), "Replay outcome should have a receipt"
        replay_receipt_norm = normalize_receipt(replay_outcome.receipt)

        # -- Compare normalised receipts --
        assert live_receipt_norm == replay_receipt_norm, (
            f"Receipt mismatch.\n"
            f"  Live:   {live_receipt_norm}\n"
            f"  Replay: {replay_receipt_norm}"
        )

        # Verify that source field is correctly set.
        assert live_outcome.receipt.source == "live", (
            f"Live receipt source should be 'live', "
            f"got {live_outcome.receipt.source!r}"
        )
        assert replay_outcome.receipt.source == "replay", (
            f"Replay receipt source should be 'replay', "
            f"got {replay_outcome.receipt.source!r}"
        )
