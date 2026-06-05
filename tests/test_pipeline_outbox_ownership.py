"""Pipeline outbox ownership tests.

Verifies that the pipeline correctly distinguishes between owned and
unowned outbox rows returned by ``create_outbox_item()``.  When the
storage layer returns an existing terminal or active row, the pipeline
must skip adapter delivery and return a ``DeliveryOutcome`` with
``status="skipped"`` and ``failure_kind=OUTBOX_NOT_OWNED``.

Also covers the replay attempt-identity rule: replay computes
``max(existing attempt_number) + 1`` so it never reclaims or mutates
live rows, and ownership checks apply to replay just as they do to
live delivery.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    DeliveryStrategy,
    stable_delivery_plan_id,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ADAPTER_ID = "fake_presentation"
_ROUTE_ID = "route-ownership-test"
_EVENT_KIND = "message.created"


@pytest.fixture
def fake_presentation() -> FakePresentationAdapter:
    return FakePresentationAdapter(adapter_id=_ADAPTER_ID, channel="ch-out")


@pytest.fixture
def route() -> Route:
    return Route(
        id=_ROUTE_ID,
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=(_EVENT_KIND,),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter=_ADAPTER_ID, channel="ch-out")],
    )


def _compute_plan_id(event_id: str) -> str:
    """Compute the deterministic delivery plan ID the pipeline will use."""
    target = RouteTarget(adapter=_ADAPTER_ID, channel="ch-out")
    return stable_delivery_plan_id(event_id, target, route_id=_ROUTE_ID, target_index=0)


def _make_plan(event_id: str, route: Route) -> DeliveryPlan:
    """Build a DeliveryPlan matching the test route for deliver_to_targets."""
    target = RouteTarget(adapter=_ADAPTER_ID, channel="ch-out")
    plan_id = stable_delivery_plan_id(
        event_id, target, route_id=route.id, target_index=0
    )
    return DeliveryPlan(
        plan_id=plan_id,
        event_id=event_id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
    )


async def _seed_outbox(
    storage: SQLiteStorage,
    *,
    event_id: str,
    status: str,
    worker_id: str | None = None,
) -> DeliveryOutboxItem:
    """Create and persist an outbox row with the given status.

    For terminal statuses (sent, dead_lettered, cancelled, abandoned) or
    queued, we first create the row as ``in_progress`` then transition it.
    """
    plan_id = _compute_plan_id(event_id)
    now = datetime.now(timezone.utc)
    item = DeliveryOutboxItem(
        outbox_id=f"obox-seed-{event_id[:12]}",
        event_id=event_id,
        route_id=_ROUTE_ID,
        delivery_plan_id=plan_id,
        target_adapter=_ADAPTER_ID,
        target_channel="ch-out",
        attempt_number=1,
        status="in_progress",
        worker_id=worker_id or "seed:worker",
        locked_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )
    created = await storage.create_outbox_item(item)

    if status == "sent":
        await storage.mark_outbox_sent(created.outbox_id, receipt_id="rcpt-seed")
    elif status == "dead_lettered":
        await storage.mark_outbox_dead_lettered(
            created.outbox_id,
            failure_kind="adapter_permanent",
            error_summary="seeded failure",
        )
    elif status == "cancelled":
        await storage.mark_outbox_cancelled(
            created.outbox_id, error_summary="seeded cancellation"
        )
    elif status == "abandoned":
        await storage.mark_outbox_abandoned(
            created.outbox_id, error_summary="seeded abandonment"
        )
    elif status == "queued":
        await storage.mark_outbox_queued(created.outbox_id, receipt_id="rcpt-seed")
    elif status == "in_progress":
        # Already in_progress — possibly with a different worker_id.
        pass
    elif status == "pending":
        # pending rows are reclaimable; create_outbox_item will reclaim them.
        pass
    elif status == "retry_wait":
        await storage.mark_outbox_retry_wait(
            created.outbox_id,
            next_attempt_at=(now + timedelta(minutes=1)).isoformat(),
            failure_kind="adapter_transient",
            error_summary="seeded transient failure",
        )
    else:
        raise ValueError(f"Unsupported seed status: {status}")

    # Re-read to get the current status.
    updated = await storage.get_outbox_item(created.outbox_id)
    assert updated is not None, f"Seeded outbox item not found: {created.outbox_id}"
    return updated


# ===================================================================
# Test: terminal row → pipeline skips delivery
# ===================================================================


class TestPipelineSkipsTerminalRow:
    """Pipeline must not deliver when the outbox row is in a terminal state."""

    @pytest.mark.parametrize(
        "terminal_status",
        ["sent", "dead_lettered", "cancelled", "abandoned"],
    )
    async def test_skips_delivery_for_terminal_row(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
        terminal_status: str,
    ) -> None:
        event_id = f"evt-terminal-{terminal_status}-001"
        await _seed_outbox(temp_storage, event_id=event_id, status=terminal_status)

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.OUTBOX_NOT_OWNED
            assert outcome.receipt is None
            assert "terminal:" in (outcome.error or "")
            # Adapter must NOT have been called.
            assert len(fake_presentation.delivered_payloads) == 0
        finally:
            await runner.stop()


# ===================================================================
# Test: queued row → pipeline skips delivery
# ===================================================================


class TestPipelineSkipsQueuedRow:
    """Pipeline must not deliver when the outbox row is queued."""

    async def test_skips_delivery_for_queued_row(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-queued-001"
        await _seed_outbox(temp_storage, event_id=event_id, status="queued")

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.OUTBOX_NOT_OWNED
            assert outcome.receipt is None
            assert "active:queued" in (outcome.error or "")
            assert len(fake_presentation.delivered_payloads) == 0
        finally:
            await runner.stop()


# ===================================================================
# Test: in_progress row owned by another worker → pipeline skips
# ===================================================================


class TestPipelineSkipsOtherWorkerInProgress:
    """Pipeline must not deliver when the outbox row is in_progress but
    owned by another worker."""

    async def test_skips_delivery_for_other_worker_in_progress(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-other-worker-001"
        await _seed_outbox(
            temp_storage,
            event_id=event_id,
            status="in_progress",
            worker_id="other:worker:123",
        )

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.OUTBOX_NOT_OWNED
            assert outcome.receipt is None
            assert "active:other_worker" in (outcome.error or "")
            assert len(fake_presentation.delivered_payloads) == 0
        finally:
            await runner.stop()


# ===================================================================
# Test: reclaimed pending row → pipeline proceeds with delivery
# ===================================================================


class TestPipelineProceedsForReclaimedPending:
    """Pipeline must proceed when a pending row is reclaimed."""

    async def test_proceeds_for_reclaimed_pending_row(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-reclaim-pending-001"
        # Create a pending row that the pipeline will reclaim.
        plan_id = _compute_plan_id(event_id)
        item = DeliveryOutboxItem(
            outbox_id=f"obox-pending-{event_id[:12]}",
            event_id=event_id,
            route_id=_ROUTE_ID,
            delivery_plan_id=plan_id,
            target_adapter=_ADAPTER_ID,
            target_channel="ch-out",
            attempt_number=1,
            status="pending",
        )
        await temp_storage.create_outbox_item(item)

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            # Successful delivery — not skipped.
            assert outcome.status in ("success", "queued")
            assert outcome.failure_kind is None
            # Adapter must have been called.
            assert len(fake_presentation.delivered_payloads) == 1
        finally:
            await runner.stop()


# ===================================================================
# Test: reclaimed retry_wait row → pipeline proceeds with delivery
# ===================================================================


class TestPipelineProceedsForReclaimedRetryWait:
    """Pipeline must proceed when a retry_wait row is reclaimed."""

    async def test_proceeds_for_reclaimed_retry_wait_row(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-reclaim-retry-wait-001"
        await _seed_outbox(temp_storage, event_id=event_id, status="retry_wait")

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status in ("success", "queued")
            assert outcome.failure_kind is None
            assert len(fake_presentation.delivered_payloads) == 1
        finally:
            await runner.stop()


# ===================================================================
# Test: no finalization for skipped rows
# ===================================================================


class TestPipelineNoFinalizeSkippedRow:
    """Pipeline must not finalize (mark_outbox_*) outbox rows that were
    skipped due to ownership failure."""

    async def test_does_not_finalize_skipped_row(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-no-finalize-001"
        seeded = await _seed_outbox(temp_storage, event_id=event_id, status="sent")
        # Record the outbox state before the pipeline runs.
        before = await temp_storage.get_outbox_item(seeded.outbox_id)
        assert before is not None
        assert before.status == "sent"

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.handle_ingress(event)

            assert len(outcomes) == 1
            assert outcomes[0].status == "skipped"

            # The outbox row must be unchanged — no finalization attempted.
            after = await temp_storage.get_outbox_item(seeded.outbox_id)
            assert after is not None
            assert after.status == "sent"
            assert after.updated_at == before.updated_at
        finally:
            await runner.stop()


# ===================================================================
# Replay collision tests
# ===================================================================


async def _seed_outbox_with_attempt(
    storage: SQLiteStorage,
    *,
    event_id: str,
    status: str,
    attempt_number: int,
    worker_id: str | None = None,
) -> DeliveryOutboxItem:
    """Seed an outbox row with an explicit attempt_number."""
    plan_id = _compute_plan_id(event_id)
    now = datetime.now(timezone.utc)

    if status == "pending":
        # Create directly as pending — no worker_id or lease.
        item = DeliveryOutboxItem(
            outbox_id=f"obox-seed-a{attempt_number}-{event_id[:12]}",
            event_id=event_id,
            route_id=_ROUTE_ID,
            delivery_plan_id=plan_id,
            target_adapter=_ADAPTER_ID,
            target_channel="ch-out",
            attempt_number=attempt_number,
            status="pending",
        )
        created = await storage.create_outbox_item(item)
    else:
        item = DeliveryOutboxItem(
            outbox_id=f"obox-seed-a{attempt_number}-{event_id[:12]}",
            event_id=event_id,
            route_id=_ROUTE_ID,
            delivery_plan_id=plan_id,
            target_adapter=_ADAPTER_ID,
            target_channel="ch-out",
            attempt_number=attempt_number,
            status="in_progress",
            worker_id=worker_id or "seed:worker",
            locked_at=now.isoformat(),
            lease_until=(now + timedelta(minutes=5)).isoformat(),
        )
        created = await storage.create_outbox_item(item)

        if status == "sent":
            await storage.mark_outbox_sent(created.outbox_id, receipt_id="rcpt-seed")
        elif status == "retry_wait":
            await storage.mark_outbox_retry_wait(
                created.outbox_id,
                next_attempt_at=(now + timedelta(minutes=1)).isoformat(),
                failure_kind="adapter_transient",
                error_summary="seeded transient failure",
            )
        elif status == "in_progress":
            pass
        else:
            raise ValueError(f"Unsupported seed status: {status}")

    updated = await storage.get_outbox_item(created.outbox_id)
    assert updated is not None
    return updated


class TestReplayWithExistingTerminalAttempt1CreatesAttempt2:
    """Pre-create a sent outbox row with attempt_number=1.  Run replay.
    Verify: adapter called, new outbox row with attempt_number=2."""

    async def test_creates_attempt_2(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-replay-terminal-1"
        await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="sent", attempt_number=1
        )

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, _make_plan(event_id, route))],
                source="replay",
            )
            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status in ("success", "queued")
            assert outcome.receipt is not None
            assert outcome.receipt.source == "replay"
            assert len(fake_presentation.delivered_payloads) == 1

            # Verify a new attempt 2 row was created.
            all_rows = await temp_storage.list_outbox_items_for_event(event_id)
            attempts = sorted(r.attempt_number for r in all_rows)
            assert 2 in attempts
        finally:
            await runner.stop()


class TestReplayWithExistingTerminalAttempts1And2CreatesAttempt3:
    """Pre-create sent rows with attempt 1 and 2.  Verify replay creates
    attempt 3."""

    async def test_creates_attempt_3(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-replay-terminal-12"
        await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="sent", attempt_number=1
        )
        await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="sent", attempt_number=2
        )

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, _make_plan(event_id, route))],
                source="replay",
            )
            assert len(outcomes) == 1
            outcome = outcomes[0]
            assert outcome.status in ("success", "queued")
            assert outcome.receipt is not None
            assert len(fake_presentation.delivered_payloads) == 1

            all_rows = await temp_storage.list_outbox_items_for_event(event_id)
            attempts = sorted(r.attempt_number for r in all_rows)
            assert 3 in attempts
        finally:
            await runner.stop()


class TestReplayDoesNotReclaimLivePendingAttempt1:
    """Pre-create a pending outbox row with attempt_number=1.  Run replay.
    Verify: attempt 1 is still pending (unchanged), attempt 2 is created
    as the replay row."""

    async def test_pending_unchanged_new_attempt_created(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-replay-pending-1"
        seeded = await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="pending", attempt_number=1
        )
        assert seeded.status == "pending"

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, _make_plan(event_id, route))],
                source="replay",
            )
            assert len(outcomes) == 1
            assert outcomes[0].status in ("success", "queued")
            assert len(fake_presentation.delivered_payloads) == 1

            # Original pending row must still be pending.
            original = await temp_storage.get_outbox_item(seeded.outbox_id)
            assert original is not None
            assert original.status == "pending"

            # A new attempt 2 row must exist.
            all_rows = await temp_storage.list_outbox_items_for_event(event_id)
            attempts = sorted(r.attempt_number for r in all_rows)
            assert 2 in attempts
        finally:
            await runner.stop()


class TestReplayDoesNotReclaimLiveRetryWaitAttempt1:
    """Pre-create a retry_wait outbox row with attempt_number=1.  Run replay.
    Verify: attempt 1 is still retry_wait (unchanged), attempt 2 is created
    as the replay row."""

    async def test_retry_wait_unchanged_new_attempt_created(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-replay-retry-wait-1"
        seeded = await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="retry_wait", attempt_number=1
        )
        assert seeded.status == "retry_wait"

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, _make_plan(event_id, route))],
                source="replay",
            )
            assert len(outcomes) == 1
            assert outcomes[0].status in ("success", "queued")
            assert len(fake_presentation.delivered_payloads) == 1

            # Original retry_wait row must still be retry_wait.
            original = await temp_storage.get_outbox_item(seeded.outbox_id)
            assert original is not None
            assert original.status == "retry_wait"

            # A new attempt 2 row must exist.
            all_rows = await temp_storage.list_outbox_items_for_event(event_id)
            attempts = sorted(r.attempt_number for r in all_rows)
            assert 2 in attempts
        finally:
            await runner.stop()


class TestReplayWithExistingActiveAttempt2OwnedByOther:
    """Pre-create an in_progress row with attempt 2 owned by another worker,
    and a terminal attempt 1 row.  Replay computes max+1=3 and creates a
    fresh row there — it must NOT reclaim or mutate the other worker's
    attempt 2 row.  Delivery succeeds via the fresh attempt 3 row."""

    async def test_creates_attempt_3_without_touching_attempt_2(
        self,
        temp_storage: SQLiteStorage,
        fake_presentation: FakePresentationAdapter,
        route: Route,
    ) -> None:
        event_id = "evt-replay-other-worker-2"
        # Seed attempt 1 as sent.
        await _seed_outbox_with_attempt(
            temp_storage, event_id=event_id, status="sent", attempt_number=1
        )
        # Seed attempt 2 as in_progress owned by another worker.
        now = datetime.now(timezone.utc)
        plan_id = _compute_plan_id(event_id)
        other_item = DeliveryOutboxItem(
            outbox_id=f"obox-race-a2-{event_id[:12]}",
            event_id=event_id,
            route_id=_ROUTE_ID,
            delivery_plan_id=plan_id,
            target_adapter=_ADAPTER_ID,
            target_channel="ch-out",
            attempt_number=2,
            status="in_progress",
            worker_id="other:worker:999",
            locked_at=now.isoformat(),
            lease_until=(now + timedelta(minutes=5)).isoformat(),
        )
        await temp_storage.create_outbox_item(other_item)

        config = make_pipeline_config_for_pipeline(
            storage=temp_storage,
            router=Router(routes=[route]),
            adapters={_ADAPTER_ID: fake_presentation},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = make_event(event_id=event_id, source_channel_id="ch-0")
            outcomes = await runner.deliver_to_targets(
                event,
                [(route, _make_plan(event_id, route))],
                source="replay",
            )
            assert len(outcomes) == 1
            outcome = outcomes[0]
            # Delivery succeeds via the fresh attempt 3 row.
            assert outcome.status in ("success", "queued")
            assert len(fake_presentation.delivered_payloads) == 1

            # The other worker's attempt 2 row must be untouched.
            other_row = await temp_storage.get_outbox_item(other_item.outbox_id)
            assert other_row is not None
            assert other_row.status == "in_progress"
            assert other_row.worker_id == "other:worker:999"

            # A new attempt 3 row must exist.
            all_rows = await temp_storage.list_outbox_items_for_event(event_id)
            attempts = sorted(r.attempt_number for r in all_rows)
            assert 3 in attempts
        finally:
            await runner.stop()
