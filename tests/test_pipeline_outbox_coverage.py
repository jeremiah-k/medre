"""Targeted uncovered-line coverage tests for OutboxManager.

Covers destination metadata, storage exception handling, lease renewal,
cancel renewal, unknown terminal outcomes, attempt-number authority,
and cancelled/abandoned outbox transitions.
"""

from __future__ import annotations

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.delivery_callbacks import make_terminal_record
from tests.helpers.pipeline import make_event
from tests.helpers.storage_outbox import (
    admit_event,
    create_outbox_item_with_parent,
)

# ===================================================================
# Targeted uncovered-line coverage
# ===================================================================


class TestDestinationMetadata:
    """Cover lines 108-109: destination metadata construction when
    RouteTarget.destination is not None."""

    async def test_create_for_delivery_with_destination(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """create_for_delivery with a RouteDestination populates metadata."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.routing.models import (
            Route,
            RouteDestination,
            RouteSource,
            RouteTarget,
        )

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        dest = RouteDestination(
            kind="lxmf_destination",
            destination_hash="abc123",
            destination_name="TestNode",
            metadata={"hop_count": 3},
        )
        target = RouteTarget(adapter="mesh-1", channel=None, destination=dest)
        route = Route(
            id="route-dest-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[target],
        )
        plan = DeliveryPlan(
            plan_id="plan-dest-001",
            event_id="evt-dest-001",
            route_id="route-dest-test",
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
            capability_level="full",
            deadline=None,
        )
        event = make_event(event_id="evt-dest-001")
        await admit_event(temp_storage, event.event_id)

        ctx = await manager.create_for_delivery(
            event=event,
            route=route,
            route_plan=plan,
            target=target,
            adapter_name="mesh-1",
        )

        assert ctx.outbox_id is not None
        assert ctx.created is True
        assert ctx.skip_reason is None

        # Verify the outbox item has destination metadata.
        item = await temp_storage.get_outbox_item(ctx.outbox_id)
        assert item is not None
        assert item.target_address == "abc123"
        assert item.metadata is not None
        meta = item.metadata
        assert meta["destination_kind"] == "lxmf_destination"
        assert meta["destination_hash"] == "abc123"
        assert meta["destination_name"] == "TestNode"
        assert meta["destination_metadata"] == {"hop_count": 3}
        # Route decision metadata should also be merged in.
        assert "capability_level" in meta
        assert "delivery_strategy" in meta


class TestOutboxCreationFailed:
    """Cover lines 217-225: the except handler in create_for_delivery."""

    async def test_create_for_delivery_storage_exception(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When storage.create_outbox_item raises, skip_reason is
        outbox_creation_failed."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
        from medre.core.routing.models import Route, RouteSource, RouteTarget

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        target = RouteTarget(adapter="mesh-1", channel="ch-1")
        route = Route(
            id="route-exn-test",
            source=RouteSource(
                adapter="fake_transport",
                event_kinds=("message.created",),
                channel="ch-0",
            ),
            targets=[target],
        )
        plan = DeliveryPlan(
            plan_id="plan-exn-001",
            event_id="evt-exn-001",
            route_id="route-exn-test",
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
            capability_level="full",
            deadline=None,
        )
        event = make_event(event_id="evt-exn-001")

        # Make storage raise during outbox creation.
        original_create = temp_storage.create_outbox_item

        async def _raising_create(item):
            raise RuntimeError("simulated storage failure")

        temp_storage.create_outbox_item = _raising_create  # type: ignore[assignment]

        ctx = await manager.create_for_delivery(
            event=event,
            route=route,
            route_plan=plan,
            target=target,
            adapter_name="mesh-1",
        )

        assert ctx.skip_reason == "outbox_creation_failed"
        assert ctx.outbox_id is None
        assert ctx.created is False

        # Restore for teardown.
        temp_storage.create_outbox_item = original_create  # type: ignore[assignment]


class TestStartLeaseRenewal:
    """Cover lines 270-272: start_lease_renewal returns task when outbox
    was created."""

    async def test_start_lease_renewal_returns_task(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When outbox was created, start_lease_renewal returns an asyncio.Task."""
        import asyncio

        import pytest

        from medre.core.engine.pipeline import outbox_manager as outbox_mod
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import (
            OutboxContext,
            OutboxManager,
        )

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        ctx = OutboxContext(
            outbox_id="obox-renewal-task-001",
            created=True,
            pipeline_worker="pipeline:abc123",
            skip_reason=None,
        )

        # Shorten renewal interval so the test runs fast, but we'll
        # cancel immediately anyway.

        original_interval = outbox_mod._OUTBOX_RENEWAL_INTERVAL_SECONDS
        outbox_mod._OUTBOX_RENEWAL_INTERVAL_SECONDS = 600  # long; task won't cycle

        try:
            task = manager.start_lease_renewal(ctx)
            assert task is not None
            assert isinstance(task, asyncio.Task)
            assert not task.done()
            # Clean up.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            outbox_mod._OUTBOX_RENEWAL_INTERVAL_SECONDS = original_interval

    async def test_start_lease_renewal_no_outbox(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When no outbox was created, start_lease_renewal returns None."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import (
            OutboxContext,
            OutboxManager,
        )

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        ctx = OutboxContext(
            outbox_id=None,
            created=False,
            pipeline_worker="pipeline:abc123",
            skip_reason="outbox_creation_failed",
        )
        result = manager.start_lease_renewal(ctx)
        assert result is None


class TestCancelRenewal:
    """Cover lines 277-284: cancel_renewal static method."""

    async def test_cancel_renewal_with_task(
        self,
    ) -> None:
        """cancel_renewal with a running task cancels and awaits it."""
        import asyncio

        from medre.core.engine.pipeline.outbox_manager import OutboxManager

        _block = asyncio.Event()

        async def _long_running():
            await _block.wait()

        task = asyncio.create_task(_long_running())
        assert not task.done()

        await OutboxManager.cancel_renewal(task)
        assert task.cancelled()

    async def test_cancel_renewal_with_exception_task(
        self,
    ) -> None:
        """cancel_renewal handles tasks that end with a non-CancelledError."""
        import asyncio

        from medre.core.engine.pipeline.outbox_manager import OutboxManager

        async def _failing():
            raise RuntimeError("unexpected error in renewal")

        task = asyncio.create_task(_failing())

        # Should NOT raise — the exception is swallowed.
        await OutboxManager.cancel_renewal(task)
        assert task.done()

    async def test_cancel_renewal_none(
        self,
    ) -> None:
        """cancel_renewal with None is a no-op."""
        from medre.core.engine.pipeline.outbox_manager import OutboxManager

        await OutboxManager.cancel_renewal(None)


class TestUnknownTerminalOutcome:
    """Cover lines 361-366: unknown outcome in record_terminal logs a warning."""

    async def test_unknown_outcome_ignored(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """An unrecognized outcome string produces no receipt and no outbox mutation."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        # Create an outbox item that would be eligible for terminal.
        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-unknown-001",
            event_id="evt-unknown-001",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="mesh-1",
            target_channel="ch-1",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)

        record = make_terminal_record(
            event_id="evt-unknown-001",
            adapter="mesh-1",
            outcome="exhausted",  # valid but we'll use a different one below
            outbox_id="obox-unknown-001",
            delivery_plan_id="plan-1",
            attempt_number=1,
            native_channel_id="ch-1",
        )
        # Override outcome to an unknown value via object.__setattr__
        # since the dataclass is frozen.
        object.__setattr__(record, "outcome", "totally_unknown_outcome")

        await manager.record_terminal(record)

        # No receipt should have been created.
        receipts = await temp_storage.list_receipts_for_event("evt-unknown-001")
        assert len(receipts) == 0

        # Outbox should remain unchanged.
        item = await temp_storage.get_outbox_item("obox-unknown-001")
        assert item is not None
        assert item.status == "in_progress"


class TestAttemptNumberAuthority:
    """The terminal receipt carries the validated outbox row's
    attempt_number (the row is authoritative), not the callback's own
    numbering.
    """

    async def test_attempt_number_from_existing_item(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """When existing_item is present, its attempt_number is used."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-attempt-existing",
            event_id="evt-attempt-existing",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="mesh-1",
            target_channel="ch-1",
            status="in_progress",
            attempt_number=7,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)

        record = make_terminal_record(
            event_id="evt-attempt-existing",
            adapter="mesh-1",
            outcome="exhausted",
            outbox_id="obox-attempt-existing",
            delivery_plan_id="plan-1",
            attempt_number=7,
            native_channel_id="ch-1",
        )
        await manager.record_terminal(record)

        # Queue exhaustion proves the enqueued attempt failed: one attempt
        # receipt (failed) plus one linked lifecycle receipt (dead_lettered)
        # at the SAME attempt number, and the outbox pointer names the
        # lifecycle receipt as terminal authority.
        receipts = await temp_storage.list_receipts_for_event("evt-attempt-existing")
        assert len(receipts) == 2
        attempt_receipt = receipts[0]
        lifecycle_receipt = receipts[1]
        assert attempt_receipt.receipt_kind == "attempt"
        assert attempt_receipt.status == "failed"
        assert attempt_receipt.attempt_number == 7
        assert lifecycle_receipt.receipt_kind == "lifecycle"
        assert lifecycle_receipt.status == "dead_lettered"
        assert lifecycle_receipt.attempt_number == 7
        assert lifecycle_receipt.parent_receipt_id == attempt_receipt.receipt_id

        outbox = await temp_storage.get_outbox_item("obox-attempt-existing")
        assert outbox is not None
        assert outbox.status == "dead_lettered"
        assert outbox.receipt_id == lifecycle_receipt.receipt_id


class TestCancelledAndAbandonedTransitions:
    """Cancelled and abandoned terminal outcomes transition the outbox row
    and commit their failed receipt."""

    async def test_cancelled_outcome_marks_cancelled(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """record_terminal with outcome='cancelled' transitions outbox to cancelled."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-cancelled-001",
            event_id="evt-cancelled-001",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="mesh-1",
            target_channel="ch-1",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)

        record = make_terminal_record(
            event_id="evt-cancelled-001",
            adapter="mesh-1",
            outcome="cancelled",
            outbox_id="obox-cancelled-001",
            delivery_plan_id="plan-1",
            attempt_number=1,
            native_channel_id="ch-1",
        )
        await manager.record_terminal(record)

        # Cancellation is a lifecycle-only transition: one lifecycle receipt
        # carries the terminal state; no dispatch failure is manufactured.
        receipts = await temp_storage.list_receipts_for_event("evt-cancelled-001")
        assert len(receipts) == 1
        assert receipts[0].status == "cancelled"
        assert receipts[0].receipt_kind == "lifecycle"
        assert receipts[0].failure_kind == "adapter_transient"

        # Outbox should be cancelled, pointing at the lifecycle receipt.
        outbox = await temp_storage.get_outbox_item("obox-cancelled-001")
        assert outbox is not None
        assert outbox.status == "cancelled"
        assert outbox.receipt_id == receipts[0].receipt_id

    async def test_abandoned_outcome_marks_abandoned(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """record_terminal with outcome='abandoned' transitions outbox to abandoned."""
        from medre.core.engine.pipeline.delivery_lifecycle import (
            DeliveryLifecycleService,
        )
        from medre.core.engine.pipeline.outbox_manager import OutboxManager
        from medre.core.storage.backend import DeliveryOutboxItem

        lifecycle = DeliveryLifecycleService()
        manager = OutboxManager(temp_storage, lifecycle)

        outbox_item = DeliveryOutboxItem(
            outbox_id="obox-abandoned-001",
            event_id="evt-abandoned-001",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="mesh-1",
            target_channel="ch-1",
            status="in_progress",
            attempt_number=1,
        )
        await create_outbox_item_with_parent(temp_storage, outbox_item)

        record = make_terminal_record(
            event_id="evt-abandoned-001",
            adapter="mesh-1",
            outcome="abandoned",
            outbox_id="obox-abandoned-001",
            delivery_plan_id="plan-1",
            attempt_number=1,
            native_channel_id="ch-1",
        )
        await manager.record_terminal(record)

        # Abandonment is a lifecycle-only transition: one lifecycle receipt
        # carries the terminal state; no dispatch failure is manufactured.
        receipts = await temp_storage.list_receipts_for_event("evt-abandoned-001")
        assert len(receipts) == 1
        assert receipts[0].status == "abandoned"
        assert receipts[0].receipt_kind == "lifecycle"
        assert receipts[0].failure_kind == "adapter_transient"

        # Outbox should be abandoned, pointing at the lifecycle receipt.
        outbox = await temp_storage.get_outbox_item("obox-abandoned-001")
        assert outbox is not None
        assert outbox.status == "abandoned"
        assert outbox.receipt_id == receipts[0].receipt_id
