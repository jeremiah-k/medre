"""Pipeline outbox integration tests: outbox creation, suppression guards,
status transitions, and shutdown interaction.
"""

from __future__ import annotations

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.policies.route_policy import RoutePolicy
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import CapacityController
from tests.helpers.async_utils import wait_until
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_transport() -> FakeTransportAdapter:
    return FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    return FakePresentationAdapter(adapter_id="fake_presentation")


@pytest.fixture
def router_with_routes() -> Router:
    """Router with a single route from fake_transport to fake_presentation."""
    route = Route(
        id="route-transport-to-presentation",
        source=RouteSource(
            adapter="fake_transport", event_kinds=("message.created",), channel="ch-0"
        ),
        targets=[RouteTarget(adapter="fake_presentation", channel="ch-out")],
    )
    return Router(routes=[route])


# ===================================================================
# Outbox creation tests
# ===================================================================


class _ZeroCapacityLimits:
    """Limits-like object with zero delivery capacity."""

    max_inflight_deliveries: int = 0
    max_inflight_replay_events: int = 0
    delivery_acquire_timeout_seconds: float = 0.1


class TestOutboxCreation:
    """Outbox should be created for accepted deliveries and suppressed for
    policy/loop/capacity-rejected targets."""

    async def test_outbox_created_for_accepted_target(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """A successful delivery creates an outbox item."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-accepted-001")

        try:
            await runner.handle_ingress(event)

            # Outbox should exist for this delivery.
            items = await temp_storage.list_outbox_items(
                status_filter=[
                    "sent",
                    "queued",
                    "in_progress",
                    "pending",
                    "retry_wait",
                    "dead_lettered",
                ],
            )
            assert len(items) == 1
            # The item should reference our event.
            matching = [i for i in items if i.event_id == "obox-accepted-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")
            assert matching[0].target_adapter == "fake_presentation"
        finally:
            await runner.stop()

    async def test_no_outbox_for_policy_suppressed(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Policy-suppressed targets should NOT create outbox items."""

        policy = RoutePolicy(
            allowed_source_adapters=("some_other_adapter",),
        )
        route = Route(
            id="route-policy-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
            policy=policy,
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-policy-sup-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Should be policy_suppressed.
            assert any(
                o.status == "skipped"
                and o.failure_kind is not None
                and "policy" in str(o.failure_kind).lower()
                for o in outcomes
            )

            # No outbox item should be created for this event.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-policy-sup-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()

    async def test_no_outbox_for_loop_suppressed(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Self-loop suppressed targets should NOT create outbox items."""
        route = Route(
            id="route-self-loop",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            # Target back to source adapter = self-loop.
            targets=[RouteTarget(adapter="fake_transport")],
        )
        router = Router(routes=[route])

        transport = FakeTransportAdapter(adapter_id="fake_transport", channel="ch-0")
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={
                "fake_transport": transport,
                "fake_presentation": fake_presentation,
            },
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-loop-sup-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Verify loop suppression.
            assert any(o.status == "skipped" for o in outcomes)

            # No outbox item for this event.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-loop-sup-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()

    async def test_no_outbox_for_capacity_rejected(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Capacity-rejected targets should NOT create outbox items."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)

        # Create a capacity controller that immediately rejects.
        cc = CapacityController(limits=_ZeroCapacityLimits())
        runner.set_capacity_controller(cc)
        await runner.start()

        event = make_event(event_id="obox-cap-rej-001")

        try:
            outcomes = await runner.handle_ingress(event)
            # Should be capacity rejected.
            assert any(
                o.status == "permanent_failure"
                and o.failure_kind is not None
                and "capacity" in str(o.failure_kind).lower()
                for o in outcomes
            )

            # No outbox item for this event (capacity reject happens before
            # outbox creation phase).
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-cap-rej-001"]
            assert len(matching) == 0
        finally:
            await runner.stop()


# ===================================================================
# Outbox status transitions
# ===================================================================


class TestOutboxStatusTransitions:
    """Outbox status updates based on delivery outcome."""

    async def test_successful_delivery_marks_sent(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Successful synchronous delivery marks outbox as sent."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-sent-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items(
                status_filter=["sent"],
            )
            matching = [i for i in items if i.event_id == "obox-sent-001"]
            assert len(matching) == 1
            assert matching[0].status == "sent"
        finally:
            await runner.stop()


class TestNoRetryPolicyDeadLetters:
    """When retry_policy is None, transient failures dead-letter the outbox item."""

    async def test_transient_failure_with_no_retry_policy_dead_letters(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """A retryable transient failure with no retry policy dead-letters the
        outbox item instead of scheduling retry_wait."""
        from medre.core.contracts.adapter import AdapterDeliveryResult

        class TransientFailAdapter(FakePresentationAdapter):
            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                raise ConnectionError("transient failure for no-retry-policy test")

        adapter = TransientFailAdapter(adapter_id="transient_fail")
        route = Route(
            id="route-no-retry",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="transient_fail")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"transient_fail": adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-no-retry-001")

        try:
            outcomes = await runner.handle_ingress(event)
            assert any(o.status in ("transient_failure", "failed") for o in outcomes)

            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-no-retry-001"]
            assert len(matching) == 1
            assert (
                matching[0].status == "dead_lettered"
            ), f"Expected dead_lettered, got {matching[0].status}"
        finally:
            await runner.stop()

    async def test_queued_delivery_marks_queued(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Queue-based delivery marks outbox as queued."""
        from medre.core.contracts.adapter import (
            AdapterDeliveryResult,
        )

        # Create a queue-based fake adapter.
        class QueuedFakeAdapter(FakePresentationAdapter):
            """Adapter that returns delivery_status='enqueued'."""

            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                self.delivered_payloads.append(result)
                return AdapterDeliveryResult(
                    native_message_id=None,
                    delivery_status="enqueued",
                )

        queued_adapter = QueuedFakeAdapter(adapter_id="fake_presentation")

        route = Route(
            id="route-queued-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[RouteTarget(adapter="fake_presentation")],
        )
        router = Router(routes=[route])

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router,
            adapters={"fake_presentation": queued_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-queued-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items(
                status_filter=["queued"],
            )
            matching = [i for i in items if i.event_id == "obox-queued-001"]
            assert len(matching) == 1
            assert matching[0].status == "queued"
        finally:
            await runner.stop()


# ===================================================================
# Live delivery claim race protection
# ===================================================================


class TestLiveDeliveryClaimRace:
    """Pipeline creates outbox items as in_progress with a lease to prevent
    the retry worker from claiming them before the live adapter attempt
    finishes."""

    async def test_in_progress_not_claimable_by_retry_worker(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
    ) -> None:
        """A live in_progress item with an active lease should not be claimable."""
        import asyncio

        from medre.core.contracts.adapter import AdapterDeliveryResult

        class BlockingAdapter(FakePresentationAdapter):
            def __init__(self) -> None:
                super().__init__(adapter_id="fake_presentation")
                self.release = asyncio.Event()

            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                self.delivered_payloads.append(result)
                await self.release.wait()
                return AdapterDeliveryResult(
                    native_message_id=f"msg-{result.event_id}",
                    native_channel_id=result.target_channel,
                )

        blocking_adapter = BlockingAdapter()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": blocking_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-race-001")

        try:
            ingress_task = asyncio.create_task(runner.handle_ingress(event))

            # Wait until the outbox item reaches in_progress status.
            item = None

            async def _find_in_progress() -> bool:
                nonlocal item
                matches = [
                    i
                    for i in await temp_storage.list_outbox_items()
                    if i.event_id == "obox-race-001"
                ]
                if matches and matches[0].status == "in_progress":
                    item = matches[0]
                    return True
                return False

            await wait_until(_find_in_progress, timeout=2.0)

            assert item is not None, "Outbox item should reach in_progress"

            # Try to claim with retry worker while lease is live — must fail
            assert item.locked_at is not None
            claimed = await temp_storage.claim_due_outbox_items(
                now=item.locked_at,  # within lease window
                worker_id="retry-worker",
                lease_seconds=30,
                limit=10,
            )
            assert not any(
                c.outbox_id == item.outbox_id for c in claimed
            ), "Live in_progress item with valid lease must not be claimed"

            # Release the adapter and let delivery complete
            blocking_adapter.release.set()
            await ingress_task
        finally:
            await runner.stop()

    async def test_in_progress_lifecycle_transitions(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Live delivery transitions: in_progress -> sent/queued/retry_wait/dead_lettered."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-lifecycle-001")

        try:
            await runner.handle_ingress(event)

            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-lifecycle-001"]
            assert len(matching) == 1

            # Synchronous successful delivery: should end at "sent".
            item = matching[0]
            assert item.status == "sent"
            # Terminal status clears lease.
            assert item.locked_at is None
            assert item.lease_until is None
            assert item.worker_id is None
        finally:
            await runner.stop()


# ===================================================================
# Shutdown-related outbox behavior
# ===================================================================


class TestOutboxShutdownBehavior:
    """Outbox behavior during shutdown."""

    async def test_shutdown_after_send_succeeds_leaves_sent_outbox(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        fake_presentation: FakePresentationAdapter,
    ) -> None:
        """Delivery completed before shutdown leaves outbox item as sent."""
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-shutdown-001")

        try:
            # Process event through pipeline
            await runner.handle_ingress(event)
        finally:
            await runner.stop()

        # After shutdown, outbox should have a sent item (the delivery
        # completed before shutdown).
        items = await temp_storage.list_outbox_items()
        matching = [i for i in items if i.event_id == "obox-shutdown-001"]
        assert len(matching) == 1
        # Delivery completed normally, so status should be sent.
        assert matching[0].status == "sent"


# ===================================================================
# Lease renewal
# ===================================================================


class TestLeaseRenewal:
    """Live delivery leases should be renewable and prevent reclaim."""

    async def test_renew_outbox_lease_method(self, temp_storage: SQLiteStorage) -> None:
        """renew_outbox_lease should extend the lease on an in_progress item."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-renew-001",
            event_id="evt-renew",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:testworker",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-renew-001", "pipeline:testworker", new_lease
        )
        assert result is True

        updated = await temp_storage.get_outbox_item("obox-renew-001")
        assert updated is not None
        assert updated.lease_until == new_lease

    async def test_renew_outbox_lease_wrong_worker(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """renew_outbox_lease should fail when the worker_id doesn't match."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-wrong-001",
            event_id="evt-wrong",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:owner",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-wrong-001", "pipeline:other", new_lease
        )
        assert result is False

    async def test_renew_outbox_lease_not_in_progress(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """renew_outbox_lease should fail when item is not in_progress."""
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-sent-001",
            event_id="evt-sent",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="pending",
        )
        await temp_storage.create_outbox_item(item)
        # Transition to sent via claim → mark_sent (Pattern A).
        await temp_storage.claim_due_outbox_items(
            now=now.isoformat(), worker_id="w1", lease_seconds=30, limit=10
        )
        await temp_storage.mark_outbox_sent("obox-sent-001", receipt_id="rcpt-sent")

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-sent-001", "pipeline:worker", new_lease
        )
        assert result is False

    async def test_renewal_prevents_claim_during_long_delivery(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
    ) -> None:
        """A slow delivery should complete and reach a terminal outbox status.

        Uses a slow adapter to verify that the outbox item transitions
        correctly through in_progress to sent/queued even when delivery
        takes non-trivial time.  Note: this test does not exercise the
        lease-renewal path (the adapter is not slow enough to cross a
        30s renewal boundary); it validates end-to-end outbox lifecycle
        for a multi-second delivery.
        """
        import asyncio

        from medre.core.contracts.adapter import AdapterDeliveryResult

        class SlowAdapter(FakePresentationAdapter):
            """Adapter that simulates a slow send (like Meshtastic)."""

            def __init__(self) -> None:
                super().__init__(adapter_id="fake_presentation")
                self._deliver_event = asyncio.Event()

            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                self.delivered_payloads.append(result)
                # Simulate a slow send — wait for the signal.
                await asyncio.sleep(0.1)
                return AdapterDeliveryResult(
                    native_message_id=f"slow-{result.event_id}",
                    native_channel_id=result.target_channel,
                )

        slow_adapter = SlowAdapter()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": slow_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-slow-001")

        try:
            outcomes = await runner.handle_ingress(event)

            # Delivery should succeed.
            assert any(o.status in ("success", "queued") for o in outcomes)

            # Verify the outbox item ended in a terminal status.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-slow-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")
        finally:
            await runner.stop()

    async def test_renew_outbox_lease_after_queued_returns_false(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After in_progress -> queued, renew_outbox_lease must return False.

        This verifies that the renewal loop in the pipeline will stop
        once the item transitions to queued (adapter-local queue acceptance).
        """
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-renew-queued",
            event_id="evt-rq",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:w1",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        # Transition to queued.
        await temp_storage.mark_outbox_queued("obox-renew-queued", receipt_id="rcpt-q")

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-renew-queued", "pipeline:w1", new_lease
        )
        assert result is False

    async def test_renew_outbox_lease_after_retry_wait_returns_false(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After in_progress -> retry_wait, renew_outbox_lease must return False.

        Verifies the renewal loop stops when the item enters retry_wait.
        """
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-renew-rw",
            event_id="evt-rw",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:w1",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        next_at = (now + timedelta(seconds=120)).isoformat()
        await temp_storage.mark_outbox_retry_wait(
            "obox-renew-rw",
            next_attempt_at=next_at,
            failure_kind="adapter_transient",
        )

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-renew-rw", "pipeline:w1", new_lease
        )
        assert result is False

    async def test_renew_outbox_lease_after_dead_lettered_returns_false(
        self, temp_storage: SQLiteStorage
    ) -> None:
        """After in_progress -> dead_lettered, renew_outbox_lease must return False.

        Verifies the renewal loop stops when the item is dead-lettered.
        """
        from datetime import datetime, timedelta, timezone

        from medre.core.storage.backend import DeliveryOutboxItem

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-renew-dl",
            event_id="evt-dl",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:w1",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        await temp_storage.mark_outbox_dead_lettered(
            "obox-renew-dl",
            failure_kind="adapter_permanent",
        )

        new_lease = (now + timedelta(seconds=1800)).isoformat()
        result = await temp_storage.renew_outbox_lease(
            "obox-renew-dl", "pipeline:w1", new_lease
        )
        assert result is False


# ===================================================================
# Regression: targeted outbox lookup with >10 noise rows
# ===================================================================


class TestTargetedOutboxLookupRegression:
    """Verify that _record_outbound_native_ref uses a targeted outbox lookup
    instead of scanning, so that the correct row transitions to ``sent`` even
    when more than 10 unrelated queued/in_progress rows exist."""

    async def test_matching_outbox_transitions_sent_despite_many_noise_rows(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Create 15 noise outbox rows + 1 target row, then call
        _record_outbound_native_ref and assert only the target transitions."""
        import uuid
        from datetime import datetime, timezone

        from medre.core.contracts.adapter import OutboundNativeRefRecord
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.events.canonical import DeliveryReceipt
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Router
        from medre.core.storage.backend import DeliveryOutboxItem

        TARGET_EVENT_ID = "evt-target-regression"
        TARGET_PLAN_ID = "plan-target-regression"
        TARGET_ADAPTER = "adapter-target"
        TARGET_CHANNEL = "ch-target"
        TARGET_ROUTE_ID = "route-target"

        # -- 1. Create 15 noise outbox items (queued + in_progress) --------
        for i in range(15):
            noise_item = DeliveryOutboxItem(
                outbox_id=f"obox-noise-{i:03d}",
                event_id=f"evt-noise-{i:03d}",
                route_id=f"route-noise-{i:03d}",
                delivery_plan_id=f"plan-noise-{i:03d}",
                target_adapter=f"adapter-noise-{i:03d}",
                target_channel=f"ch-noise-{i:03d}",
                status="in_progress",
            )
            await temp_storage.create_outbox_item(noise_item)
            if i % 2 == 0:
                # Reach "queued" via in_progress → mark_queued (Pattern C).
                await temp_storage.mark_outbox_queued(f"obox-noise-{i:03d}")

        # -- 2. Create the matching outbox item (queued) --------------------
        # Reach "queued" via in_progress → mark_queued (Pattern C).
        target_item = DeliveryOutboxItem(
            outbox_id="obox-target-regression",
            event_id=TARGET_EVENT_ID,
            route_id=TARGET_ROUTE_ID,
            delivery_plan_id=TARGET_PLAN_ID,
            target_adapter=TARGET_ADAPTER,
            target_channel=TARGET_CHANNEL,
            status="in_progress",
        )
        await temp_storage.create_outbox_item(target_item)
        await temp_storage.mark_outbox_queued("obox-target-regression")

        # -- 3. Create a "queued" receipt so _append_queued_to_sent_receipt
        #         can find it and inherit plan/route context. ---------------
        now = datetime.now(tz=timezone.utc)
        queued_receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-queued-{uuid.uuid4()}",
            event_id=TARGET_EVENT_ID,
            delivery_plan_id=TARGET_PLAN_ID,
            target_adapter=TARGET_ADAPTER,
            target_channel=TARGET_CHANNEL,
            route_id=TARGET_ROUTE_ID,
            status="queued",
            error=None,
            failure_kind=None,
            adapter_message_id=None,
            next_retry_at=None,
            created_at=now,
            attempt_number=1,
            parent_receipt_id=None,
            source="live",
        )
        await temp_storage.append_receipt(queued_receipt)

        # -- 4. Build a minimal PipelineRunner and call the production path -
        router = Router(routes=[])
        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        record = OutboundNativeRefRecord(
            event_id=TARGET_EVENT_ID,
            adapter=TARGET_ADAPTER,
            native_channel_id=TARGET_CHANNEL,
            native_message_id="native-msg-target-regression",
            delivery_plan_id=TARGET_PLAN_ID,
            outbox_id="obox-target-regression",
        )
        await runner._record_outbound_native_ref(record)

        # -- 5. Assert: target outbox item is now "sent" -------------------
        updated_target = await temp_storage.get_outbox_item("obox-target-regression")
        assert updated_target is not None, "Target outbox item should exist"
        assert (
            updated_target.status == "sent"
        ), f"Expected 'sent', got '{updated_target.status}'"

        # -- 6. Assert: all noise rows remain unchanged --------------------
        for i in range(15):
            noise = await temp_storage.get_outbox_item(f"obox-noise-{i:03d}")
            assert noise is not None, f"Noise item {i} should still exist"
            expected_status = "queued" if i % 2 == 0 else "in_progress"
            assert noise.status == expected_status, (
                f"Noise item {i} should remain '{expected_status}', "
                f"got '{noise.status}'"
            )


# ===================================================================
# Lease-renewal resilience
# ===================================================================


class TestLeaseRenewalResilience:
    """Transient errors during lease renewal must not permanently stop the
    renewal loop; the loop should continue retrying until the delivery
    completes and the outer finally block cancels the renewal task.
    """

    async def test_renewal_continues_after_transient_exception(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """renew_outbox_lease raising once must NOT permanently kill the
        renewal loop.  After the transient error, renewal should be called
        again and the delivery should complete normally with the outbox
        reaching a terminal status.
        """
        import asyncio

        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.engine.pipeline import outbox_manager as outbox_mod

        # Short-circuit the renewal interval for a fast test.
        monkeypatch.setattr(outbox_mod, "_OUTBOX_RENEWAL_INTERVAL_SECONDS", 0.05)

        call_count = 0

        class BlockingAdapter(FakePresentationAdapter):
            """Adapter that blocks until signalled, simulating a slow delivery."""

            def __init__(self) -> None:
                super().__init__(adapter_id="fake_presentation")
                self._release = asyncio.Event()

            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                self.delivered_payloads.append(result)
                await self._release.wait()
                return AdapterDeliveryResult(
                    native_message_id=f"msg-{result.event_id}",
                    native_channel_id=result.target_channel,
                )

        blocking_adapter = BlockingAdapter()
        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": blocking_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        original_renew = temp_storage.renew_outbox_lease

        async def _flaky_renew(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient db blip")
            return await original_renew(*args, **kwargs)

        temp_storage.renew_outbox_lease = _flaky_renew  # type: ignore[assignment]

        event = make_event(event_id="obox-resilient-001")

        try:
            ingress_task = asyncio.create_task(runner.handle_ingress(event))

            # Wait for ≥2 renewal cycles to fire (interval is 50ms).
            await wait_until(lambda: call_count >= 2, timeout=2.0)

            # Release the adapter so delivery can complete.
            blocking_adapter._release.set()
            outcomes = await ingress_task

            # Delivery should succeed.
            assert any(o.status in ("success", "queued") for o in outcomes)

            # The renewal method was called more than once, proving the loop
            # continued after the first transient exception.
            assert call_count >= 2, (
                f"Expected renew_outbox_lease to be called ≥2 times, "
                f"got {call_count}"
            )

            # Outbox should reach a terminal status.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-resilient-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")
        finally:
            await runner.stop()

    async def test_cancelled_error_exits_renewal_loop(
        self,
        temp_storage: SQLiteStorage,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """asyncio.CancelledError must exit the renewal loop (not be swallowed
        by the transient-error handler)."""
        import asyncio

        from medre.core.engine.pipeline import outbox_manager as outbox_mod
        from medre.core.storage.backend import DeliveryOutboxItem

        monkeypatch.setattr(outbox_mod, "_OUTBOX_RENEWAL_INTERVAL_SECONDS", 0.05)

        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        item = DeliveryOutboxItem(
            outbox_id="obox-cancel-test",
            event_id="evt-cancel",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="fake_presentation",
            status="in_progress",
            worker_id="pipeline:w1",
            lease_until=(now + timedelta(seconds=60)).isoformat(),
            locked_at=now.isoformat(),
        )
        await temp_storage.create_outbox_item(item)

        async def _cancel_on_first(*args, **kwargs):
            raise asyncio.CancelledError("simulated cancellation")

        temp_storage.renew_outbox_lease = _cancel_on_first  # type: ignore[assignment]

        # Build a minimal runner to call the renewal starter.
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Router

        config = PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)

        task = runner._start_outbox_lease_renewal(
            "obox-cancel-test", True, "pipeline:w1"
        )
        assert task is not None

        # Wait for the renewal task to finish.  It should raise
        # CancelledError, which ends the task.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    async def test_finalization_still_runs_after_renewal_exception(
        self,
        temp_storage: SQLiteStorage,
        router_with_routes: Router,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even when renewal keeps failing, the finalization cleanup must
        still run and the outbox must transition to a terminal status."""
        import asyncio

        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.engine.pipeline import outbox_manager as outbox_mod

        monkeypatch.setattr(outbox_mod, "_OUTBOX_RENEWAL_INTERVAL_SECONDS", 0.05)

        renew_call_count = 0

        async def _always_fail_renew(*args, **kwargs):
            nonlocal renew_call_count
            renew_call_count += 1
            raise RuntimeError("persistent failure")

        temp_storage.renew_outbox_lease = _always_fail_renew  # type: ignore[assignment]

        class SlowAdapter(FakePresentationAdapter):
            """Adapter slow enough for ≥1 renewal cycle to fire."""

            def __init__(self) -> None:
                super().__init__(adapter_id="fake_presentation")

            async def deliver(
                self, result: RenderingResult
            ) -> AdapterDeliveryResult | None:
                self.delivered_payloads.append(result)
                await asyncio.sleep(0.15)
                return AdapterDeliveryResult(
                    native_message_id=f"msg-{result.event_id}",
                    native_channel_id=result.target_channel,
                )

        slow_adapter = SlowAdapter()

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=router_with_routes,
            adapters={"fake_presentation": slow_adapter},
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = make_event(event_id="obox-finalize-001")

        try:
            outcomes = await runner.handle_ingress(event)

            # Delivery should still succeed (renewal failure is non-fatal).
            assert any(o.status in ("success", "queued") for o in outcomes)

            # Outbox should reach a terminal status despite renewal failures.
            items = await temp_storage.list_outbox_items()
            matching = [i for i in items if i.event_id == "obox-finalize-001"]
            assert len(matching) == 1
            assert matching[0].status in ("sent", "queued")

            # Renewal was attempted multiple times (proving the loop
            # continued after the first error rather than permanently dying).
            assert (
                renew_call_count >= 2
            ), f"Expected ≥2 renewal attempts, got {renew_call_count}"
        finally:
            await runner.stop()


# ===================================================================
# record_terminal attempt_number preservation
# ===================================================================


class TestRecordTerminalAttemptNumber:
    """Verify OutboxManager.record_terminal preserves attempt_number from
    the outbox item rather than defaulting to 1.

    Regression: the original implementation omitted attempt_number from
    the terminal receipt, causing every terminal receipt to record
    attempt_number=1 regardless of how many retries occurred.
    """

    async def test_terminal_receipt_uses_outbox_attempt_number(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Outbox item at attempt_number=3 → terminal receipt has attempt_number=3."""
        from medre.core.contracts.adapter import QueueTerminalRecord
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        # Create an outbox item at attempt_number=3 (simulates retry #3).
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-attempt-3",
            event_id="evt-terminal-attempt",
            route_id="route-1",
            delivery_plan_id="plan-terminal",
            target_adapter="mesh-1",
            target_channel="0",
            status="in_progress",
            attempt_number=3,
        )
        await temp_storage.create_outbox_item(outbox_item)

        record = QueueTerminalRecord(
            event_id="evt-terminal-attempt",
            adapter="mesh-1",
            native_channel_id="0",
            outcome="exhausted",
            error="retry budget exhausted",
            outbox_id="obox-attempt-3",
            delivery_plan_id="plan-terminal",
        )
        await manager.record_terminal(record)

        # Verify receipt has attempt_number=3, NOT the default 1.
        receipts = await temp_storage.list_receipts_for_event(
            "evt-terminal-attempt",
        )
        assert len(receipts) == 1
        assert receipts[0].attempt_number == 3, (
            f"Expected attempt_number=3, got {receipts[0].attempt_number}"
        )
        assert receipts[0].status == "failed"
        assert receipts[0].outbox_id == "obox-attempt-3"
