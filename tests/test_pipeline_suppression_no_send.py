"""Suppression-path integration tests proving adapter send is never called.

Verifies that every suppression gate (capability skip, plan skip, listen-only,
disabled route, loop suppression) produces correct DeliveryOutcome/evidence
without invoking the target adapter's ``deliver()`` method.
"""

from __future__ import annotations

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ===================================================================
# a) Capability skip (text=False) does not call adapter send
# ===================================================================


class TestCapabilitySkipDoesNotCallAdapterSend:
    """text=False capability on a text event suppresses delivery entirely."""

    async def test_capability_skip_does_not_call_adapter_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """An adapter with text=False suppresses message.text events.

        The PipelineRunner's Phase 2.5 capability check produces
        status='skipped' / CAPABILITY_SUPPRESSED and the adapter's
        deliver() is never called.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=False)

        route = Route(
            id="cap-skip-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="cap-skip-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert (
                len(outcomes) == 1
            ), f"Expected exactly 1 outcome, got {len(outcomes)}"
            outcome = outcomes[0]

            assert (
                outcome.status == "skipped"
            ), f"Expected status='skipped', got status={outcome.status!r}"
            assert (
                outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            ), f"Expected CAPABILITY_SUPPRESSED, got {outcome.failure_kind}"
            # Adapter deliver() was never invoked.
            assert len(adapter.delivered_payloads) == 0, (
                f"Expected 0 delivered_payloads, but adapter received "
                f"{len(adapter.delivered_payloads)} calls to deliver()"
            )
        finally:
            await runner.stop()


# ===================================================================
# b) Plan-level skip does not call adapter send
# ===================================================================


class TestPlanLevelSkipDoesNotCallAdapterSend:
    """Plan with primary_strategy.method='skip' bypasses adapter entirely."""

    async def test_plan_level_skip_does_not_call_adapter_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Reactions='unsupported' on a message.reacted event causes plan skip.

        The planner creates a skip plan.  The pipeline returns
        status='skipped' / CAPABILITY_SUPPRESSED without calling the adapter.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="plan-skip-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="plan-skip-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert (
                len(outcomes) == 1
            ), f"Expected exactly 1 outcome, got {len(outcomes)}"
            outcome = outcomes[0]

            assert (
                outcome.status == "skipped"
            ), f"Expected status='skipped', got status={outcome.status!r}"
            assert (
                outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            ), f"Expected CAPABILITY_SUPPRESSED, got {outcome.failure_kind}"
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0, (
                f"Expected 0 delivered_payloads, but adapter received "
                f"{len(adapter.delivered_payloads)} calls to deliver()"
            )
        finally:
            await runner.stop()


# ===================================================================
# c) Listen-only route suppression does not call adapter send
# ===================================================================


class TestListenOnlyRouteSuppressionNoSend:
    """Meshtastic adapter with outbound_mode='listen_only' suppresses sends."""

    async def test_listen_only_route_suppression_no_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A listen_only Meshtastic adapter raises AdapterPermanentError.

        The pipeline processes the event through the full delivery path.
        When deliver() is called on the listen_only adapter it raises
        AdapterPermanentError.  The outcome is permanent_failure /
        ADAPTER_PERMANENT — but the fake client's send_text is never
        invoked (no radio transmission occurs).

        Additionally, the adapter's delivered_payloads list stays empty
        because the outbound gate fires before the payload is appended.
        """
        mesh_config = MeshtasticConfig(
            adapter_id="listen-mesh",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = FakeMeshtasticAdapter(mesh_config)

        route = Route(
            id="listen-only-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="listen-mesh", channel="0")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"listen-mesh": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="listen-only-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert (
                len(outcomes) == 1
            ), f"Expected exactly 1 outcome, got {len(outcomes)}"
            outcome = outcomes[0]

            # The listen_only gate produces an adapter permanent failure.
            assert outcome.status == "permanent_failure", (
                f"Expected status='permanent_failure', got "
                f"status={outcome.status!r}"
            )
            assert (
                outcome.failure_kind is DeliveryFailureKind.ADAPTER_PERMANENT
            ), f"Expected ADAPTER_PERMANENT, got {outcome.failure_kind}"

            # The adapter's delivered_payloads stays empty because the
            # listen_only gate fires before appending.
            assert len(adapter.delivered_payloads) == 0, (
                f"Expected 0 delivered_payloads for listen_only adapter, "
                f"but got {len(adapter.delivered_payloads)}"
            )

            # The outbound gate suppression counter was incremented.
            assert adapter._outbound_gate_suppressed == 1, (
                f"Expected outbound_gate_suppressed=1, "
                f"got {adapter._outbound_gate_suppressed}"
            )
        finally:
            await runner.stop()


# ===================================================================
# d) Disabled route does not call adapter send
# ===================================================================


class TestDisabledRouteSuppressionNoSend:
    """A disabled route produces zero outcomes — no delivery attempted."""

    async def test_disabled_route_suppression_no_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Route with enabled=False is filtered by Router.match().

        No routes match, so handle_ingress returns an empty outcomes list
        and the adapter's deliver() is never called.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")

        route = Route(
            id="disabled-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
            enabled=False,
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
            event_id="disabled-route-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            # No routes matched — empty outcomes.
            assert (
                len(outcomes) == 0
            ), f"Expected 0 outcomes for disabled route, got {len(outcomes)}"
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0, (
                f"Expected 0 delivered_payloads for disabled route, "
                f"but adapter received {len(adapter.delivered_payloads)} calls"
            )
        finally:
            await runner.stop()


# ===================================================================
# e) Loop suppression does not call adapter send
# ===================================================================


class TestLoopSuppressionNoSend:
    """Self-loop guard suppresses delivery without calling the adapter."""

    async def test_loop_suppression_no_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Event sourced from adapter X routed back to adapter X is suppressed.

        The PipelineRunner's self-loop guard returns status='skipped' /
        LOOP_SUPPRESSED and never calls the adapter's deliver().
        """
        adapter = FakePresentationAdapter(adapter_id="loop-dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="loop-route",
            source=RouteSource(
                adapter="loop-dest",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="loop-dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"loop-dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="loop-sup-001",
            event_kind="message.text",
            source_adapter="loop-dest",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)

            assert (
                len(outcomes) == 1
            ), f"Expected exactly 1 outcome, got {len(outcomes)}"
            outcome = outcomes[0]

            assert (
                outcome.status == "skipped"
            ), f"Expected status='skipped', got status={outcome.status!r}"
            assert (
                outcome.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            ), f"Expected LOOP_SUPPRESSED, got {outcome.failure_kind}"
            # Adapter never invoked.
            assert len(adapter.delivered_payloads) == 0, (
                f"Expected 0 delivered_payloads for loop-suppressed event, "
                f"but adapter received {len(adapter.delivered_payloads)} calls"
            )
        finally:
            await runner.stop()


# ===================================================================
# f) Suppressed outcomes produce evidence bundles
# ===================================================================


class TestSuppressedOutcomeProducesEvidence:
    """Suppressed/skipped outcomes produce persistent evidence bundles."""

    async def test_suppressed_outcome_produces_evidence(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A capability-suppressed delivery persists a receipt in storage.

        collect_evidence_bundle reads the receipt and produces an
        incident_summary with suppressed_count=1.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=False)

        route = Route(
            id="evidence-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="evidence-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"
        finally:
            await runner.stop()

        # Verify the receipt was persisted.
        stored_receipts = await temp_storage.list_receipts_for_event(
            event.event_id,
        )
        assert (
            len(stored_receipts) == 1
        ), f"Expected exactly 1 persisted receipt, got {len(stored_receipts)}"
        assert stored_receipts[0].status == "suppressed", (
            f"Expected receipt status='suppressed', "
            f"got {stored_receipts[0].status!r}"
        )

        # Verify evidence bundle via storage_path.
        db_path = temp_storage._db_path
        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event.event_id,
        )
        storage_section = report["sections"]["storage"]
        assert (
            storage_section["status"] == "passed"
        ), f"Storage section error: {storage_section.get('error')}"
        summary = storage_section["data"]["incident_summary"]
        assert (
            summary["suppressed_count"] == 1
        ), f"Expected suppressed_count=1, got {summary['suppressed_count']}"


# ===================================================================
# g) Suppressed outcomes are not like failed sends
# ===================================================================


class TestSuppressedOutcomeNotLikeFailedSend:
    """Suppressed outcomes have status='skipped' not 'failed'."""

    async def test_suppressed_outcome_not_like_failed_send(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A capability-suppressed outcome is 'skipped', not 'failed'.

        The receipt status is 'suppressed' (not 'failed' or 'sent').
        The DeliveryOutcome status is 'skipped' (not 'transient_failure'
        or 'permanent_failure').
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=False)

        route = Route(
            id="not-failed-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="not-failed-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            outcome = outcomes[0]

            # Outcome status must be 'skipped', not a failure variant.
            assert outcome.status == "skipped", (
                f"Suppressed outcome status must be 'skipped', "
                f"got {outcome.status!r}"
            )
            assert (
                outcome.status != "transient_failure"
            ), "Suppressed outcome must NOT look like a transient failure"
            assert (
                outcome.status != "permanent_failure"
            ), "Suppressed outcome must NOT look like a permanent failure"

            # The receipt must be 'suppressed', not 'failed'.
            assert outcome.receipt is not None, "Outcome must carry a receipt"
            assert outcome.receipt.status == "suppressed", (
                f"Receipt status must be 'suppressed', "
                f"got {outcome.receipt.status!r}"
            )
            assert (
                outcome.receipt.status != "failed"
            ), "Suppressed receipt must NOT have status 'failed'"

            # failure_kind must be a suppression variant, not adapter error.
            assert (
                outcome.failure_kind is DeliveryFailureKind.CAPABILITY_SUPPRESSED
            ), f"Expected CAPABILITY_SUPPRESSED, got {outcome.failure_kind}"
            assert (
                outcome.failure_kind is not DeliveryFailureKind.ADAPTER_PERMANENT
            ), "Suppressed outcome must NOT be ADAPTER_PERMANENT"
            assert (
                outcome.failure_kind is not DeliveryFailureKind.ADAPTER_TRANSIENT
            ), "Suppressed outcome must NOT be ADAPTER_TRANSIENT"

            # Not retryable.
            assert (
                outcome.failure_kind.is_retryable is False
            ), "Suppressed outcome must NOT be retryable"
        finally:
            await runner.stop()

    async def test_loop_suppressed_not_like_adapter_failure(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Loop-suppressed outcome is 'skipped', not an adapter failure."""
        adapter = FakePresentationAdapter(adapter_id="loop-dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="loop-not-fail-route",
            source=RouteSource(
                adapter="loop-dest",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="loop-dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"loop-dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="loop-not-fail-001",
            event_kind="message.text",
            source_adapter="loop-dest",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            outcome = outcomes[0]

            assert (
                outcome.status == "skipped"
            ), f"Loop-suppressed outcome must be 'skipped', got {outcome.status!r}"
            assert (
                outcome.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            ), f"Expected LOOP_SUPPRESSED, got {outcome.failure_kind}"
            assert (
                outcome.failure_kind.is_retryable is False
            ), "Loop-suppressed must NOT be retryable"

            # Receipt is suppressed, not failed.
            assert outcome.receipt is not None
            assert outcome.receipt.status == "suppressed", (
                f"Expected receipt status 'suppressed', "
                f"got {outcome.receipt.status!r}"
            )
            assert outcome.receipt.failure_kind == "loop_suppressed", (
                f"Expected receipt failure_kind 'loop_suppressed', "
                f"got {outcome.receipt.failure_kind!r}"
            )
        finally:
            await runner.stop()


# ===================================================================
# h) Suppressed deliveries are not enqueued for retry
# ===================================================================


class TestSuppressedDeliveriesNotEnqueuedForRetry:
    """Suppressed deliveries produce no outbox items for retry."""

    async def test_suppressed_deliveries_not_enqueued_for_retry(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Capability-suppressed events create zero outbox items.

        The suppression gates fire before Phase 3.5 (outbox creation),
        so suppressed events never enter the retry queue.
        """
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(text=False)

        route = Route(
            id="no-retry-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="no-retry-001",
            event_kind="message.text",
            source_adapter="src",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"

            # Check that no outbox items were created for this event.
            outbox_items = await temp_storage.list_outbox_items()
            suppressed_items = [i for i in outbox_items if i.event_id == event.event_id]
            assert len(suppressed_items) == 0, (
                f"Expected 0 outbox items for suppressed event, "
                f"got {len(suppressed_items)}"
            )
        finally:
            await runner.stop()

    async def test_plan_skip_not_enqueued_for_retry(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Plan-level skip also produces zero outbox items."""
        adapter = FakePresentationAdapter(adapter_id="dest")
        adapter._capabilities = AdapterCapabilities(
            text=True,
            reactions="unsupported",
        )

        route = Route(
            id="plan-skip-no-retry-route",
            source=RouteSource(
                adapter="src",
                event_kinds=("message.reacted",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="dest")],
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
            event_id="plan-skip-no-retry-001",
            event_kind="message.reacted",
            source_adapter="src",
            source_channel_id="ch-0",
            payload={"emoji": "\U0001f44d"},
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"

            outbox_items = await temp_storage.list_outbox_items()
            plan_skip_items = [i for i in outbox_items if i.event_id == event.event_id]
            assert len(plan_skip_items) == 0, (
                f"Expected 0 outbox items for plan-skip event, "
                f"got {len(plan_skip_items)}"
            )
        finally:
            await runner.stop()

    async def test_loop_suppressed_not_enqueued_for_retry(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Loop-suppressed events produce zero outbox items."""
        adapter = FakePresentationAdapter(adapter_id="loop-dest")
        adapter._capabilities = AdapterCapabilities(text=True)

        route = Route(
            id="loop-no-retry-route",
            source=RouteSource(
                adapter="loop-dest",
                event_kinds=("message.text",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="loop-dest")],
        )
        router = Router(routes=[route])
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"loop-dest": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(
            event_id="loop-no-retry-001",
            event_kind="message.text",
            source_adapter="loop-dest",
            source_channel_id="ch-0",
        )

        try:
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"

            outbox_items = await temp_storage.list_outbox_items()
            loop_items = [i for i in outbox_items if i.event_id == event.event_id]
            assert len(loop_items) == 0, (
                f"Expected 0 outbox items for loop-suppressed event, "
                f"got {len(loop_items)}"
            )
        finally:
            await runner.stop()
