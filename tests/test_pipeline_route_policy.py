"""Pipeline integration tests for route-policy enforcement.

Proves that policy suppression actually fires through PipelineRunner._deliver_one(),
not just the pure evaluator in medre.core.policies.route_policy.  These tests
exercise the real pipeline with fake adapters and storage, verifying:

* Policy-denied target produces status="skipped" with failure_kind=POLICY_SUPPRESSED.
* Suppressed receipt is persisted with route_id, delivery_plan_id, target_adapter,
  target_channel.
* Renderer and adapter delivery path are NOT invoked on suppression.
* Same route with multiple targets can deliver one and suppress another.
* RouteStats records policy_suppressed.
* Evidence bundle surfaces delivery_state_by_target with suppressed target context.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.policies.route_policy import RoutePolicy
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


# ===================================================================
# 1. Policy-denied produces skipped / POLICY_SUPPRESSED
# ===================================================================


class TestPolicyDeniedOutcome:
    """Policy-denied target produces the correct DeliveryOutcome."""

    @pytest.mark.asyncio
    async def test_policy_denied_produces_skipped_with_failure_kind(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """allowed_source_adapters excluding the actual source → suppressed."""
        adapter = FakePresentationAdapter(adapter_id="dest")

        policy = RoutePolicy(allowed_source_adapters=("other_source",))
        route = Route(
            id="policy-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dest")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="policy-001",
            source_adapter="src",
            source_channel_id=None,
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.POLICY_SUPPRESSED
            assert outcome.target_adapter == "dest"
            assert outcome.route_id == "policy-route"
            assert outcome.event_id == "policy-001"
            assert outcome.error is not None
            assert "policy_suppressed" in outcome.error
            assert "source_adapter_not_allowed" in outcome.error
        finally:
            await runner.stop()


# ===================================================================
# 2. Suppressed receipt persisted with full context
# ===================================================================


class TestPolicySuppressedReceipt:
    """Suppressed receipt persisted to storage with route/plan/target context."""

    @pytest.mark.asyncio
    async def test_receipt_persisted_with_route_plan_target_fields(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Receipt has route_id, delivery_plan_id, target_adapter, target_channel."""
        adapter = FakePresentationAdapter(adapter_id="radio")

        policy = RoutePolicy(allowed_dest_adapters=("matrix",))
        route = Route(
            id="dest-filter-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="radio", channel="ch-3")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"radio": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="receipt-001",
            source_adapter="src",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1

            # Check outcome receipt fields.
            receipt = outcomes[0].receipt
            assert receipt is not None
            assert receipt.status == "suppressed"
            assert receipt.failure_kind == "policy_suppressed"
            assert receipt.route_id == "dest-filter-route"
            assert receipt.target_adapter == "radio"
            assert receipt.target_channel == "ch-3"
            assert receipt.delivery_plan_id is not None

            # Check stored receipt in database.
            stored = await temp_storage.list_receipts_for_event("receipt-001")
            assert len(stored) == 1
            assert stored[0].status == "suppressed"
            assert stored[0].failure_kind == "policy_suppressed"
            assert stored[0].route_id == "dest-filter-route"
            assert stored[0].target_adapter == "radio"
            assert stored[0].target_channel == "ch-3"
        finally:
            await runner.stop()


# ===================================================================
# 3. Renderer/adapter NOT called on policy suppression
# ===================================================================


class TestPolicySuppressedNoSideEffects:
    """Policy denial must not invoke renderer or adapter delivery."""

    @pytest.mark.asyncio
    async def test_adapter_not_called_on_suppression(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """FakePresentationAdapter.deliver() is never called when policy denies."""
        adapter = FakePresentationAdapter(adapter_id="blocked")

        policy = RoutePolicy(sender_allowlist=("allowed_sender",))
        route = Route(
            id="sender-filter",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="blocked")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"blocked": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Event from a sender NOT in the allowlist.
        event = make_event(
            event_id="no-sidefx-001",
            source_adapter="src",
            source_channel_id=None,
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.POLICY_SUPPRESSED

            # Adapter was never called — no rendered payloads delivered.
            assert len(adapter.delivered_payloads) == 0
            assert len(adapter.received_events) == 0
        finally:
            await runner.stop()


# ===================================================================
# 4. Policy-allowed delivery proceeds normally
# ===================================================================


class TestPolicyAllowedProceeds:
    """When policy allows the event, delivery proceeds normally."""

    @pytest.mark.asyncio
    async def test_allowed_event_delivered_successfully(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event matching all policy allowlists is delivered normally."""
        adapter = FakePresentationAdapter(adapter_id="target")

        policy = RoutePolicy(
            allowed_source_adapters=("src",),
            sender_allowlist=("node-1",),
        )
        route = Route(
            id="allow-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="target")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"target": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        # node-1 is in sender_allowlist, src is in allowed_source_adapters.
        event = make_event(
            event_id="allow-001",
            source_adapter="src",
            source_channel_id=None,
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "success"
            assert outcomes[0].failure_kind is None
            assert outcomes[0].target_adapter == "target"

            # Adapter was called.
            assert len(adapter.delivered_payloads) == 1
        finally:
            await runner.stop()


# ===================================================================
# 5. Mixed targets: deliver one, suppress another
# ===================================================================


class TestMixedTargetsDeliverAndSuppress:
    """Same route with multiple targets: policy allows one, suppresses another."""

    @pytest.mark.asyncio
    async def test_one_target_delivered_one_suppressed(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """allowed_dest_adapters permits only one of two targets."""
        adapter_a = FakePresentationAdapter(adapter_id="adapter_a")
        adapter_b = FakePresentationAdapter(adapter_id="adapter_b")

        # Only adapter_a is in the allowed dest list.
        policy = RoutePolicy(allowed_dest_adapters=("adapter_a",))
        route = Route(
            id="mixed-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[
                RouteTarget(adapter="adapter_a"),
                RouteTarget(adapter="adapter_b"),
            ],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"adapter_a": adapter_a, "adapter_b": adapter_b},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="mixed-001",
            source_adapter="src",
            source_channel_id=None,
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 2

            by_status = {o.status: o for o in outcomes}

            # adapter_a was allowed and delivered.
            assert "success" in by_status
            success = by_status["success"]
            assert success.target_adapter == "adapter_a"

            # adapter_b was suppressed.
            assert "skipped" in by_status
            skipped = by_status["skipped"]
            assert skipped.target_adapter == "adapter_b"
            assert skipped.failure_kind is DeliveryFailureKind.POLICY_SUPPRESSED

            # Verify adapter call counts.
            assert len(adapter_a.delivered_payloads) == 1
            assert len(adapter_b.delivered_payloads) == 0

            # Both receipts stored — one sent, one suppressed.
            stored = await temp_storage.list_receipts_for_event("mixed-001")
            assert len(stored) == 2
            by_adapter = {r.target_adapter: r for r in stored}
            assert by_adapter["adapter_a"].status == "sent"
            assert by_adapter["adapter_b"].status == "suppressed"
            assert by_adapter["adapter_b"].failure_kind == "policy_suppressed"
        finally:
            await runner.stop()


# ===================================================================
# 6. RouteStats records policy_suppressed
# ===================================================================


class TestPolicySuppressedRouteStats:
    """RouteStats.counter for policy_suppressed increments on denial."""

    @pytest.mark.asyncio
    async def test_route_stats_policy_suppressed_counter(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """RouteStats records policy_suppressed for the denied route."""
        adapter = FakePresentationAdapter(adapter_id="dest")

        policy = RoutePolicy(channel_allowlist=("ch-allowed",))
        route = Route(
            id="stats-policy-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="dest", channel="ch-blocked")],
            policy=policy,
        )
        router = Router(routes=[route])
        stats = RouteStats()

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"dest": adapter},
        )
        config.route_stats = stats
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="stats-policy-001",
            source_adapter="src",
        )

        try:
            await runner.handle_ingress(event)

            snap = stats.snapshot()
            assert "stats-policy-route" in snap
            assert snap["stats-policy-route"]["policy_suppressed"] == 1
            assert snap["stats-policy-route"]["delivered"] == 0
            assert snap["stats-policy-route"]["failed"] == 0
        finally:
            await runner.stop()


# ===================================================================
# 7. Evidence bundle: delivery_state_by_target with suppressed target
# ===================================================================


class TestPolicySuppressedEvidence:
    """Evidence bundle surfaces delivery_state_by_target for policy suppression."""

    @pytest.mark.asyncio
    async def test_delivery_state_by_target_includes_suppressed_target(
        self,
        tmp_path: Path,
    ) -> None:
        """collect_evidence_bundle shows suppressed target with target-keyed context."""
        from medre.core.supervision.accounting import RuntimeAccounting
        from medre.runtime.evidence._bundle import collect_evidence_bundle
        from tests.helpers.bridge import make_pipeline_config

        db_path = str(tmp_path / "policy_suppression_evidence.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        adapter = FakePresentationAdapter(adapter_id="ev-dest")

        policy = RoutePolicy(room_allowlist=("!allowed:server",))
        route = Route(
            id="ev-policy-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="ev-dest", channel="ch-x")],
            policy=policy,
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()
        route_stats = RouteStats()

        config = make_pipeline_config(
            storage=storage,
            router=router,
            adapters={"ev-dest": adapter},
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        # Event from a room NOT in the allowlist.
        event = make_event(
            event_id="ev-policy-001",
            source_adapter="src",
            source_channel_id="!blocked:server",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
            assert outcomes[0].failure_kind is DeliveryFailureKind.POLICY_SUPPRESSED
        finally:
            await runner.stop()
            await storage.close()

        # Collect evidence bundle and verify delivery_state_by_target.
        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-policy-001",
        )

        storage_section = report["sections"]["storage"]
        assert storage_section["status"] == "passed", (
            f"Storage section error: {storage_section.get('error')}"
        )

        data = storage_section["data"]
        summary = data["incident_summary"]
        assert summary is not None

        # Exactly one suppressed receipt.
        assert summary["suppressed_count"] == 1
        assert summary["first_failure_kind"] == "policy_suppressed"

        # delivery_state_by_target contains the suppressed target.
        dsbt = summary["delivery_state_by_target"]
        assert isinstance(dsbt, dict)
        assert len(dsbt) == 1

        target_state = next(iter(dsbt.values()))
        assert target_state["status"] == "suppressed"
        assert target_state["failure_kind"] == "policy_suppressed"
        assert target_state["retryable"] is False
        assert target_state["target_adapter"] == "ev-dest"
        assert target_state["target_channel"] == "ch-x"
        assert "route_id" in target_state

    @pytest.mark.asyncio
    async def test_delivery_state_by_target_keyed_by_target_components(
        self,
        tmp_path: Path,
    ) -> None:
        """delivery_state_by_target key contains adapter+channel+route_id components."""
        from medre.core.supervision.accounting import RuntimeAccounting
        from medre.runtime.evidence._bundle import collect_evidence_bundle
        from tests.helpers.bridge import make_pipeline_config

        db_path = str(tmp_path / "policy_target_key.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        adapter = FakePresentationAdapter(adapter_id="key-dest")

        policy = RoutePolicy(sender_allowlist=("nobody",))
        route = Route(
            id="key-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="key-dest", channel="key-ch")],
            policy=policy,
        )
        router = Router(routes=[route])
        accounting = RuntimeAccounting()

        config = make_pipeline_config(
            storage=storage,
            router=router,
            adapters={"key-dest": adapter},
            accounting=accounting,
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="key-evt-001",
            source_adapter="src",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
        finally:
            await runner.stop()
            await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="key-evt-001",
        )

        data = report["sections"]["storage"]["data"]
        dsbt = data["incident_summary"]["delivery_state_by_target"]
        assert len(dsbt) == 1

        # The key should be a JSON-parseable string containing target components.
        key = next(iter(dsbt.keys()))
        assert "key-dest" in key
        # Target-channel in the key (or JSON null if channel was None).
        assert "key-ch" in key
