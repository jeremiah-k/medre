"""Post-erasure retry decisions consume persisted admission-time route bounds.

Ceiling enforcement retires an outbox generation's frozen dispatch context
(inline metadata and payload hash cleared, snapshot rows deleted).  The
remaining retry decisions for such an erased route — the retry-budget
arithmetic in ``DeliveryLifecycleService.reconcile_retry_claim`` — MUST use
the content-free bounds persisted at admission
(``delivery_route_retry_bounds``) and MUST NOT consult the worker's current
configuration.  Generations admitted without bounds (historical predecessor
rows and routes admitted without a resolved retry policy) reconcile without
bounds: persisted evidence settles exactly as before and no further attempt
is scheduled — never a reconstruction from globals.

These tests pin the admission write, the post-erasure consumption, the
global-default drift guard, and the predecessor-row behavior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
)
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    OutboxDispatchSnapshotWrite,
    OutboxRouteRetryBounds,
    OutboxRouteRetryBoundsWrite,
)
from medre.runtime.retry import (
    _RECONCILE_ONLY_RETRY_POLICY,
    RetryWorker,
    _route_bounds_retry_policy,
)
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import admit_event

_WORKER = "retry-worker-bounds"

# The ceiling sweep's documented purge outcome for one event's outbox
# generations (see medre.core.storage.sqlite._ceiling_enforcement): inline
# dispatch metadata and payload hash are cleared and the frozen snapshot rows
# are deleted.  The bounds facts survive — they are content-free.
_PURGE_SNAPSHOT_SQL = "DELETE FROM delivery_outbox_snapshots WHERE outbox_id = ?"


# ---------------------------------------------------------------------------
# Policy resolution helper
# ---------------------------------------------------------------------------


def test_bounds_absent_resolves_reconcile_only_policy() -> None:
    """Predecessor rows reconcile without bounds; config is never consulted."""
    policy = _route_bounds_retry_policy(None)
    assert policy == _RECONCILE_ONLY_RETRY_POLICY
    assert policy.max_attempts == 1
    assert policy.jitter is False


def test_bounds_resolve_the_route_policy_exactly() -> None:
    bounds = OutboxRouteRetryBounds(
        outbox_id="obox-bounds",
        max_attempts=7,
        backoff_base=1.5,
        max_delay_seconds=300.0,
        jitter=True,
    )
    policy = _route_bounds_retry_policy(bounds)
    assert policy == RetryPolicy(
        max_attempts=7,
        backoff_base=1.5,
        max_delay_seconds=300.0,
        jitter=True,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0, "backoff_base": 2.0, "max_delay_seconds": 60.0},
        {"max_attempts": 1, "backoff_base": 0.0, "max_delay_seconds": 60.0},
        {"max_attempts": 1, "backoff_base": 2.0, "max_delay_seconds": -1.0},
    ],
)
def test_bounds_write_rejects_nonpositive_arithmetic(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        OutboxRouteRetryBoundsWrite(jitter=False, **kwargs)


# ---------------------------------------------------------------------------
# Admission: content-free facts written once with the outbox row
# ---------------------------------------------------------------------------


async def test_admission_persists_bounds_that_survive_the_purge(temp_storage):
    outbox_id = "obox-bounds-survive"
    await admit_event(temp_storage, "evt-bounds")
    stored = await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id="evt-bounds",
            route_id="route-bounds",
            delivery_plan_id="plan-bounds",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=1,
            status="pending",
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(kind="direct", context={}),
        route_retry_bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=5,
            backoff_base=2.0,
            max_delay_seconds=90.0,
            jitter=True,
        ),
    )
    assert stored.outbox_id == outbox_id
    before = await temp_storage.get_outbox_route_retry_bounds(outbox_id)
    assert before is not None
    assert (before.max_attempts, before.backoff_base, before.max_delay_seconds) == (
        5,
        2.0,
        90.0,
    )
    assert before.jitter is True
    # Simulate the ceiling sweep's purge of the frozen dispatch context; the
    # content-free bounds row is untouched by it.
    await temp_storage._write(_PURGE_SNAPSHOT_SQL, (outbox_id,))
    after = await temp_storage.get_outbox_route_retry_bounds(outbox_id)
    assert after == before


async def test_admission_without_resolved_policy_persists_no_bounds(temp_storage):
    outbox_id = "obox-no-policy"
    await admit_event(temp_storage, "evt-no-policy")
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id="evt-no-policy",
            route_id="route-no-policy",
            delivery_plan_id="plan-no-policy",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=1,
            status="pending",
        ),
    )
    assert await temp_storage.get_outbox_route_retry_bounds(outbox_id) is None


async def test_adopted_row_never_gains_bounds_from_a_later_creator(temp_storage):
    """Bounds are admission facts: an adopted row keeps its own (absent) facts."""
    outbox_id = "obox-adopted"
    await admit_event(temp_storage, "evt-adopted")
    item = DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id="evt-adopted",
        route_id="route-adopted",
        delivery_plan_id="plan-adopted",
        target_adapter="fake",
        target_channel="ch-0",
        attempt_number=1,
        status="pending",
    )
    await temp_storage.create_outbox_item(item)
    await temp_storage.create_outbox_item(
        item,
        route_retry_bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=9,
            backoff_base=2.0,
            max_delay_seconds=60.0,
            jitter=False,
        ),
    )
    assert await temp_storage.get_outbox_route_retry_bounds(outbox_id) is None


# ---------------------------------------------------------------------------
# Manager admission: the accepted plan's resolved policy freezes as facts
# ---------------------------------------------------------------------------


def _direct_plan(
    event_id: str, target: RouteTarget, *, with_policy: bool
) -> DeliveryPlan:
    route = Route(
        id=f"route-{event_id}",
        source=RouteSource(adapter=None, event_kinds=(), channel=None),
        targets=[target],
    )
    return DeliveryPlan(
        plan_id=f"plan-{event_id}",
        event_id=event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        retry_policy=(
            RetryPolicy(
                max_attempts=4,
                backoff_base=3.0,
                max_delay_seconds=120.0,
                jitter=False,
            )
            if with_policy
            else None
        ),
    )


async def test_manager_admission_persists_the_plan_policy_bounds(temp_storage):
    """The accepted plan's resolved retry policy freezes as admission facts."""
    event = make_storage_event(event_id="evt-manager")
    await temp_storage.admit_ingress(event, None, "live")
    target = RouteTarget(adapter="fake", channel="ch-0")
    plan = _direct_plan(event.event_id, target, with_policy=True)
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, _route_for(event.event_id, target), plan, target, "fake"
    )
    assert context.outbox_id is not None
    bounds = await temp_storage.get_outbox_route_retry_bounds(context.outbox_id)
    assert bounds is not None
    assert bounds == OutboxRouteRetryBounds(
        outbox_id=context.outbox_id,
        max_attempts=4,
        backoff_base=3.0,
        max_delay_seconds=120.0,
        jitter=False,
    )


async def test_manager_admission_without_policy_persists_no_bounds(temp_storage):
    event = make_storage_event(event_id="evt-manager-none")
    await temp_storage.admit_ingress(event, None, "live")
    target = RouteTarget(adapter="fake", channel="ch-0")
    plan = _direct_plan(event.event_id, target, with_policy=False)
    manager = OutboxManager(temp_storage, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, _route_for(event.event_id, target), plan, target, "fake"
    )
    assert context.outbox_id is not None
    assert await temp_storage.get_outbox_route_retry_bounds(context.outbox_id) is None


def _route_for(event_id: str, target: RouteTarget) -> Route:
    return Route(
        id=f"route-{event_id}",
        source=RouteSource(adapter=None, event_kinds=(), channel=None),
        targets=[target],
    )


# ---------------------------------------------------------------------------
# Consumption: post-erasure reconciliation policy resolution
# ---------------------------------------------------------------------------


def _capture_storage(bounds) -> MagicMock:
    """Fake worker storage exposing only the purged-path read surface."""
    storage = MagicMock(
        spec=[
            "get_metadata",
            "get",
            "delivery_status",
            "get_outbox_dispatch_context",
            "get_outbox_route_retry_bounds",
            "read_event_for_outbox",
        ]
    )
    stored = object()
    storage.get_metadata = AsyncMock(return_value=stored)
    storage.get = AsyncMock(return_value=stored)
    storage.delivery_status = AsyncMock(return_value=None)
    storage.get_outbox_dispatch_context = AsyncMock(return_value=None)
    storage.get_outbox_route_retry_bounds = AsyncMock(return_value=bounds)
    # The erased route's content authority is denied: no native send is
    # possible, only evidence reconciliation.
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="denied", event=None)
    )
    return storage


def _purged_item(**overrides) -> DeliveryOutboxItem:
    fields: dict = {
        "outbox_id": "obox-erased",
        "event_id": "evt-erased",
        "route_id": "route-erased",
        "delivery_plan_id": "plan-erased",
        "target_adapter": "target_a",
        "attempt_number": 1,
        "status": "in_progress",
        "worker_id": _WORKER,
    }
    fields.update(overrides)
    return DeliveryOutboxItem(**fields)


def _capturing_lifecycle() -> MagicMock:
    lifecycle = MagicMock()
    lifecycle.reconcile_retry_claim = AsyncMock(return_value=None)
    lifecycle.reconcile_content_denied_handoff = AsyncMock(return_value=None)
    lifecycle.settle_committed_acceptance = AsyncMock(return_value=None)
    lifecycle.list_attempt_receipts = AsyncMock(return_value=[])
    lifecycle.abandon_retry_outbox = AsyncMock(return_value=None)
    return lifecycle


async def _run_purged(worker: RetryWorker, item: DeliveryOutboxItem) -> MagicMock:
    emit = MagicMock()
    worker._emit = emit
    await worker._retry_outbox_item(item)
    return emit


@pytest.mark.parametrize("worker_max_attempts", [1, 3, 10])
async def test_post_erasure_reconcile_uses_persisted_bounds_not_worker_config(
    worker_max_attempts,
):
    """The route's persisted budget reaches reconciliation; config never does."""
    bounds = OutboxRouteRetryBounds(
        outbox_id="obox-erased",
        max_attempts=5,
        backoff_base=1.5,
        max_delay_seconds=240.0,
        jitter=True,
    )
    storage = _capture_storage(bounds)
    lifecycle = _capturing_lifecycle()
    worker = RetryWorker(
        storage=storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=worker_max_attempts,
        lifecycle=lifecycle,
    )
    item = _purged_item()

    await _run_purged(worker, item)

    storage.get_outbox_route_retry_bounds.assert_awaited_once_with("obox-erased")
    lifecycle.reconcile_retry_claim.assert_awaited_once()
    policy = lifecycle.reconcile_retry_claim.await_args.args[2]
    assert policy == RetryPolicy(
        max_attempts=5,
        backoff_base=1.5,
        max_delay_seconds=240.0,
        jitter=True,
    )
    # The bounds govern budget arithmetic only: the erased route still cannot
    # dispatch, and the denial abandon carries no transport call.
    lifecycle.abandon_retry_outbox.assert_awaited_once_with(
        storage,
        item,
        error_summary="content_authority_denied",
        expected_observation_count=None,
        expected_checkpoint_revision=None,
        expected_receipt_count=0,
        expected_observation_scope_revision=None,
    )


async def test_post_erasure_reconcile_cannot_exceed_persisted_budget():
    """A larger worker budget cannot extend an erased route's own limits."""
    bounds = OutboxRouteRetryBounds(
        outbox_id="obox-erased",
        max_attempts=1,
        backoff_base=2.0,
        max_delay_seconds=60.0,
        jitter=False,
    )
    storage = _capture_storage(bounds)
    lifecycle = _capturing_lifecycle()
    worker = RetryWorker(
        storage=storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=10,
        lifecycle=lifecycle,
    )

    await _run_purged(worker, _purged_item())

    policy = lifecycle.reconcile_retry_claim.await_args.args[2]
    assert policy.max_attempts == 1
    assert policy.jitter is False


async def test_predecessor_row_reconciles_without_bounds():
    """A row with no bounds reconciles reconcile-only; globals stay out."""
    storage = _capture_storage(None)
    lifecycle = _capturing_lifecycle()
    worker = RetryWorker(
        storage=storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=10,
        lifecycle=lifecycle,
    )

    await _run_purged(worker, _purged_item())

    policy = lifecycle.reconcile_retry_claim.await_args.args[2]
    assert policy == _RECONCILE_ONLY_RETRY_POLICY


# ---------------------------------------------------------------------------
# End-to-end drift guard: real storage, real lifecycle, real transitions
# ---------------------------------------------------------------------------


async def _seed_erased_reserved_row(
    temp_storage,
    outbox_id: str,
    *,
    bounds: OutboxRouteRetryBoundsWrite | None,
) -> DeliveryOutboxItem:
    """Admit a row, reserve an attempt, then simulate the ceiling purge.

    The reservation has no evidence: reconciliation must decide its outcome
    from the persisted bounds (or reconcile-only without bounds).
    """
    event_id = f"evt-{outbox_id}"
    await admit_event(temp_storage, event_id)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event_id,
            route_id=f"route-{outbox_id}",
            delivery_plan_id=f"plan-{outbox_id}",
            target_adapter="fake",
            target_channel="ch-0",
            attempt_number=0,
            status="in_progress",
            locked_at=datetime.now(timezone.utc).isoformat(),
            lease_until=(
                datetime.now(timezone.utc) + timedelta(seconds=60)
            ).isoformat(),
            worker_id=_WORKER,
        ),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(kind="direct", context={}),
        route_retry_bounds=bounds,
    )
    reserved = await temp_storage.reserve_outbox_attempt(outbox_id, _WORKER, 0)
    assert reserved == 1
    # Ceiling-enforcement purge: the frozen dispatch context is retired.  The
    # content-free bounds survive (asserted below); the reservation and any
    # acceptance evidence remain.
    await temp_storage._write(_PURGE_SNAPSHOT_SQL, (outbox_id,))
    item = await temp_storage.get_outbox_item(outbox_id)
    assert item is not None
    assert item.active_attempt == 1
    assert not item.metadata
    assert item.payload_hash is None
    assert (
        await temp_storage.get_outbox_dispatch_context(
            outbox_id,
            event_id=item.event_id,
            delivery_plan_id=item.delivery_plan_id,
            target_adapter=item.target_adapter,
            target_channel=item.target_channel,
        )
        is None
    )
    return item


def _erased_authority(storage) -> None:
    """Deny the purged route's content authority at the read seam.

    Ceiling enforcement has expired the event's content, so
    ``read_event_for_outbox`` denies for this erased route.
    """
    storage.read_event_for_outbox = AsyncMock(
        return_value=SimpleNamespace(status="denied", event=None)
    )


def _drift_worker(storage, *, max_attempts: int) -> RetryWorker:
    worker = RetryWorker(
        storage=storage,
        pipeline=MagicMock(),
        capacity_controller=None,
        enabled=True,
        max_attempts=max_attempts,
    )
    worker._emit = MagicMock()
    return worker


async def test_erased_route_reconciles_on_its_own_budget_not_global_drift(
    temp_storage,
):
    """Bounds allow another wait even when current config dead-letters at 1."""
    item = await _seed_erased_reserved_row(
        temp_storage,
        "obox-drift-low",
        bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=5,
            backoff_base=2.0,
            max_delay_seconds=60.0,
            jitter=False,
        ),
    )
    _erased_authority(temp_storage)
    worker = _drift_worker(temp_storage, max_attempts=1)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "retry_wait"
    assert row.next_attempt_at is not None
    # Deterministic bounds backoff: attempt 1 with backoff_base 2.0 and no
    # jitter schedules exactly 2 seconds out.
    scheduled = datetime.fromisoformat(row.next_attempt_at)
    delay = scheduled - datetime.now(timezone.utc)
    assert timedelta(seconds=1.5) < delay < timedelta(seconds=2.5)


async def test_erased_route_with_exhausted_bounds_dead_letters_despite_larger_global(
    temp_storage,
):
    item = await _seed_erased_reserved_row(
        temp_storage,
        "obox-drift-high",
        bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=1,
            backoff_base=2.0,
            max_delay_seconds=60.0,
            jitter=False,
        ),
    )
    _erased_authority(temp_storage)
    worker = _drift_worker(temp_storage, max_attempts=10)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.failure_kind == "retry_exhausted"
    assert row.next_attempt_at is None


async def test_predecessor_erased_row_dead_letters_without_bounds(temp_storage):
    """No persisted bounds: reconcile-only settles the attempt terminally."""
    item = await _seed_erased_reserved_row(
        temp_storage,
        "obox-predecessor",
        bounds=None,
    )
    _erased_authority(temp_storage)
    worker = _drift_worker(temp_storage, max_attempts=10)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "dead_lettered"
    assert row.failure_kind == "retry_exhausted"


async def test_erased_route_settles_committed_evidence_before_budget(temp_storage):
    """Committed acceptance settles under bounds regardless of the budget."""
    from medre.core.events import DeliveryReceipt

    item = await _seed_erased_reserved_row(
        temp_storage,
        "obox-drift-acceptance",
        bounds=OutboxRouteRetryBoundsWrite(
            max_attempts=1,
            backoff_base=2.0,
            max_delay_seconds=60.0,
            jitter=False,
        ),
    )
    receipt = DeliveryReceipt(
        receipt_id="rcpt-erased-acceptance",
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
        outbox_id=item.outbox_id,
        attempt_number=1,
        status="sent",
        source="retry",
    )
    await temp_storage.append_receipt(receipt)
    _erased_authority(temp_storage)
    worker = _drift_worker(temp_storage, max_attempts=1)

    await worker._retry_outbox_item(item)

    row = await temp_storage.get_outbox_item(item.outbox_id)
    assert row is not None
    assert row.status == "sent"
    assert row.receipt_id == "rcpt-erased-acceptance"
