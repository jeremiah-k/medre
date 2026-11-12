"""The final synchronous dispatch-start deadline fence.

MEDRE refuses to initiate a new local native adapter submission when the
final pre-invocation clock observation is at or after the earliest
applicable effective deadline: the immutable event content ceiling, the
frozen delivery-plan deadline riding the row's dispatch snapshot, and the
adopted target dependency deadline, composed with the plan's own deadline.
The bound is observed once from the same storage-authoritative rows the
native-start authorization checks, carried through rendering to the
adapter invocation site, and compared against the local UTC wall clock
with no await between the observation and the invocation. This does not
promise that a previously accepted native message cannot arrive
afterward, and it is not transactional with a network call.
"""

from __future__ import annotations

import ast
import inspect
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import medre.core.engine.pipeline.radio_delivery as radio_delivery_module
import medre.core.engine.pipeline.target_delivery as target_delivery_module
from medre.core.contracts.adapter import AdapterHandoffResult
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.retry_plan import reconstruct_retry_delivery_plan
from medre.core.engine.pipeline.target_delivery import (
    _DISPATCH_START_DEADLINE_ERROR,
    _AdapterDeliveryError,
    _DispatchStartDeadlineRefused,
    _dispatch_start_fenced_invoke,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.radio_text import plan_radio_text
from medre.core.rendering.renderer import RenderingResult
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, OutboxDispatchSnapshotWrite
from medre.core.storage.dispatch_bound import EffectiveDispatchBound
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import make_outbox_item
from tests.helpers.target_delivery import (
    _FakeRenderingPipeline,
    _make_event,
    _make_route_and_plan,
    _make_service,
)


def _future(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _direct_snapshot(deadline: str | None) -> OutboxDispatchSnapshotWrite:
    """Freeze the reduced direct dispatch context exactly as staging builds it."""
    return OutboxDispatchSnapshotWrite(
        kind="direct",
        context={
            "delivery_strategy": "direct",
            "capability_level": "native",
            "capability_field": None,
            "capability_reason": None,
            "deadline": deadline,
        },
    )


class _Clock(datetime):
    """Controlled pipeline clock shared by the patched delivery modules."""

    current = datetime.now(timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _fenced_clock(monkeypatch, start: datetime):
    """Patch the delivery modules' clock and start it at *start*."""
    monkeypatch.setattr(target_delivery_module, "datetime", _Clock)
    monkeypatch.setattr(radio_delivery_module, "datetime", _Clock)
    _Clock.current = start
    return _Clock


class _RecordingAdapter:
    """Adapter that records how often its native deliver() was entered."""

    platform = "test_platform"

    def __init__(self, native_message_id: str = "accepted") -> None:
        self.calls = 0
        self._native_message_id = native_message_id

    async def deliver(self, rendering_result):
        self.calls += 1
        return AdapterHandoffResult(native_message_id=self._native_message_id)


def _fenced_service(store: SQLiteStorage, event_id: str):
    """Build a TargetDeliveryService over *store* with a fixed rendering result."""
    adapter = _RecordingAdapter(native_message_id="unsafe")
    service, _ = _make_service(
        adapters={"test_adapter": adapter},
        storage=store,
        rendering_pipeline=_FakeRenderingPipeline(
            result=RenderingResult(
                event_id=event_id,
                target_adapter="test_adapter",
                target_channel=None,
                payload={"text": "hello"},
            )
        ),
    )
    return service, adapter


def _cross_second_authorization(store: SQLiteStorage, cross_to: datetime | None):
    """Advance the pipeline clock right after the awaited final authorization.

    Storage samples its own real clock, so the authorization still passes and
    only the carried bound can fence the start - exactly the gap the contract
    closes.
    """
    original = store.authorize_native_submission
    authorizations = 0

    async def cross_after_authorization(*args, **kwargs):
        nonlocal authorizations
        allowed = await original(*args, **kwargs)
        authorizations += 1
        if authorizations == 2 and cross_to is not None:
            _Clock.current = cross_to
        return allowed

    return cross_after_authorization, authorizations


# ---------------------------------------------------------------------------
# Typed bound derivation from storage authority
# ---------------------------------------------------------------------------


async def test_observed_bound_is_the_earliest_applicable_deadline(tmp_path):
    """The observation returns the earliest authoritative bound and its source."""
    store = SQLiteStorage(str(tmp_path / "bound-derivation.db"))
    await store.initialize()
    try:
        ceiling = _future(600)
        frozen = _future(120)
        earlier_dependency = _future(60)
        # Ceiling only: admission installs the ceiling and the source hold,
        # and the adopted target dependency inherits the same instant.
        await store.admit_ingress(
            make_storage_event(event_id="bound-ceiling"),
            None,
            "live",
            content_deadline=ceiling,
        )
        created = await store.create_outbox_item(
            make_outbox_item(event_id="bound-ceiling", delivery_plan_id="plan-a")
        )
        bound = await store.observe_effective_dispatch_bound(
            "bound-ceiling", created.outbox_id
        )
        assert bound is not None
        assert bound.source == "event_content_ceiling"
        assert bound.deadline == ceiling

        # A frozen plan deadline earlier than the ceiling binds first.
        await store.admit_ingress(
            make_storage_event(event_id="bound-frozen"),
            None,
            "live",
            content_deadline=ceiling,
        )
        created_frozen = await store.create_outbox_item(
            make_outbox_item(event_id="bound-frozen", delivery_plan_id="plan-b"),
            dispatch_snapshot=_direct_snapshot(frozen.isoformat()),
        )
        bound_frozen = await store.observe_effective_dispatch_bound(
            "bound-frozen", created_frozen.outbox_id
        )
        assert bound_frozen is not None
        assert bound_frozen.source == "frozen_plan_deadline"
        assert bound_frozen.deadline == frozen

        # The adopted target dependency deadline binds when it is the
        # earliest authoritative instant on the row.
        await store.admit_ingress(
            make_storage_event(event_id="bound-dependency"),
            None,
            "live",
            content_deadline=ceiling,
        )
        created_dependency = await store.create_outbox_item(
            make_outbox_item(event_id="bound-dependency", delivery_plan_id="plan-c")
        )
        await store._write(
            "UPDATE event_content_dependencies SET deadline=? "
            "WHERE owner_kind='outbox' AND owner_id=?",
            (
                earlier_dependency.isoformat(timespec="microseconds"),
                created_dependency.outbox_id,
            ),
        )
        bound_dependency = await store.observe_effective_dispatch_bound(
            "bound-dependency", created_dependency.outbox_id
        )
        assert bound_dependency is not None
        assert bound_dependency.source == "adopted_target_dependency_deadline"
        assert bound_dependency.deadline == earlier_dependency

        # With no bound installed the fence stays dormant.
        await store.admit_ingress(
            make_storage_event(event_id="bound-dormant"), None, "live"
        )
        created_dormant = await store.create_outbox_item(
            make_outbox_item(event_id="bound-dormant", delivery_plan_id="plan-d")
        )
        assert (
            await store.observe_effective_dispatch_bound(
                "bound-dormant", created_dormant.outbox_id
            )
            is None
        )
    finally:
        await store.close()


async def test_malformed_frozen_bound_refuses_instead_of_binding(tmp_path):
    """A present but malformed bound raises rather than reading as absent."""
    store = SQLiteStorage(str(tmp_path / "bound-malformed.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="bound-bad"), None, "live"
        )
        created = await store.create_outbox_item(
            make_outbox_item(event_id="bound-bad", delivery_plan_id="plan-bad"),
        )
        await store._write(
            "UPDATE delivery_outbox SET metadata=? WHERE outbox_id=?",
            ('{"deadline": "not-a-timestamp"}', created.outbox_id),
        )
        with pytest.raises(ValueError):
            await store.observe_effective_dispatch_bound("bound-bad", created.outbox_id)
        # The native-start fence refuses on the same malformed bound.
        await store._write(
            "UPDATE delivery_outbox SET status='in_progress', worker_id=?, "
            "lease_until=? WHERE outbox_id=?",
            (
                "pipeline:bad",
                _future(300).isoformat(timespec="microseconds"),
                created.outbox_id,
            ),
        )
        assert not await store.authorize_native_submission(
            "bound-bad",
            created.outbox_id,
            worker_id="pipeline:bad",
            attempt_number=1,
        )
    finally:
        await store.close()


async def test_replay_generation_without_target_dependency_still_observes_bounds(
    tmp_path,
):
    """Replay dispatch fences on the ceiling and frozen plan deadline alone."""
    store = SQLiteStorage(str(tmp_path / "bound-replay.db"))
    await store.initialize()
    try:
        ceiling = _future(3600)
        frozen = _future(600)
        await store.admit_ingress(
            make_storage_event(event_id="replay-fence"),
            None,
            "live",
            content_deadline=ceiling,
        )
        replay = replace(
            make_outbox_item(
                event_id="replay-fence",
                delivery_plan_id="plan-replay",
                target_channel=None,
            ),
            dispatch_source="replay",
            replay_run_id="run-fence",
        )
        stored = await store.create_outbox_item(
            replay,
            allocate_new_generation=True,
            dispatch_snapshot=_direct_snapshot(frozen.isoformat()),
        )
        # A replay generation never mints its own target-scoped hold.
        dependencies = await store.list_mandatory_content_dependencies("replay-fence")
        assert not any(
            kind == "outbox" and owner == stored.outbox_id
            for kind, owner, _, _ in dependencies
        )
        bound = await store.observe_effective_dispatch_bound(
            "replay-fence", stored.outbox_id
        )
        assert bound is not None
        assert bound.source == "frozen_plan_deadline"
        assert bound.deadline == frozen
        # The dispatch-start fence still refuses a replay generation past its
        # frozen plan deadline even with no target dependency to release.
        await store._write(
            "UPDATE delivery_outbox SET status='in_progress', worker_id=?, "
            "lease_until=? WHERE outbox_id=?",
            (
                "pipeline:replay",
                _future(300).isoformat(timespec="microseconds"),
                stored.outbox_id,
            ),
        )
        assert await store.authorize_native_submission(
            "replay-fence",
            stored.outbox_id,
            worker_id="pipeline:replay",
            attempt_number=1,
        )
        assert not await store.authorize_native_submission(
            "replay-fence",
            stored.outbox_id,
            worker_id="pipeline:replay",
            attempt_number=1,
            now=frozen + timedelta(seconds=1),
        )
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# The final synchronous fence at the adapter invocation site
# ---------------------------------------------------------------------------


async def _stage_owned_generation(
    store: SQLiteStorage,
    event_id: str,
    *,
    plan_deadline: datetime | None = None,
    snapshot: bool = True,
    worker_id: str = "pipeline:clock",
) -> str:
    now = datetime.now(timezone.utc)
    item = DeliveryOutboxItem(
        outbox_id=f"obox-{event_id}",
        event_id=event_id,
        delivery_plan_id="plan-001",
        route_id="route-001",
        target_adapter="test_adapter",
        attempt_number=1,
        status="in_progress",
        worker_id=worker_id,
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )
    dispatch_snapshot = (
        _direct_snapshot(plan_deadline.isoformat()) if snapshot else None
    )
    created = await store.create_outbox_item(item, dispatch_snapshot=dispatch_snapshot)
    return created.outbox_id


async def test_dispatch_start_passes_before_the_effective_bound(tmp_path, monkeypatch):
    """A start observed strictly before the bound hands off normally."""
    store = SQLiteStorage(str(tmp_path / "fence-before.db"))
    await store.initialize()
    try:
        deadline = _future(120)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        outbox_id = await _stage_owned_generation(
            store, event.event_id, plan_deadline=deadline
        )
        clock = _fenced_clock(monkeypatch, deadline - timedelta(seconds=30))
        adapter = _RecordingAdapter()
        service, _ = _make_service(
            adapters={"test_adapter": adapter},
            storage=store,
            rendering_pipeline=_FakeRenderingPipeline(
                result=RenderingResult(
                    event_id=event.event_id,
                    target_adapter="test_adapter",
                    target_channel=None,
                    payload={"text": "hello"},
                )
            ),
        )
        route, plan = _make_route_and_plan()
        plan = replace(plan, deadline=deadline)
        receipt = (
    await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
).primary_receipt
        assert receipt.status == "sent"
        assert adapter.calls == 1
        assert clock.current < deadline
    finally:
        await store.close()


@pytest.mark.parametrize("offset_seconds", [0, 1])
async def test_dispatch_start_refuses_at_or_after_the_bound(
    tmp_path, monkeypatch, offset_seconds
):
    """Refusal fires at exact equality (>=) and after, with no adapter call."""
    store = SQLiteStorage(str(tmp_path / f"fence-refuse-{offset_seconds}.db"))
    await store.initialize()
    try:
        deadline = _future(120)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        outbox_id = await _stage_owned_generation(
            store, event.event_id, plan_deadline=deadline
        )
        clock = _fenced_clock(monkeypatch, deadline - timedelta(seconds=30))
        service, adapter = _fenced_service(store, event.event_id)
        route, plan = _make_route_and_plan()
        plan = replace(plan, deadline=deadline)
        cross_after_authorization, _ = _cross_second_authorization(
            store, deadline + timedelta(seconds=offset_seconds)
        )
        monkeypatch.setattr(
            store, "authorize_native_submission", cross_after_authorization
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        assert adapter.calls == 0
        assert caught.value.error == _DISPATCH_START_DEADLINE_ERROR
        assert caught.value.failure_kind is not None
        assert caught.value.failure_kind.value == "deadline_exceeded"
        assert clock.current >= deadline
    finally:
        await store.close()


async def test_refusal_records_local_denial_evidence_and_mutates_nothing(
    tmp_path, monkeypatch
):
    """The refused start keeps no receipt and touches no accepted state."""
    store = SQLiteStorage(str(tmp_path / "fence-evidence.db"))
    await store.initialize()
    try:
        deadline = _future(120)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        outbox_id = await _stage_owned_generation(
            store, event.event_id, plan_deadline=deadline
        )
        clock = _fenced_clock(monkeypatch, deadline - timedelta(seconds=30))
        service, adapter = _fenced_service(store, event.event_id)
        route, plan = _make_route_and_plan()
        plan = replace(plan, deadline=deadline)
        cross_after_authorization, _ = _cross_second_authorization(store, deadline)
        monkeypatch.setattr(
            store, "authorize_native_submission", cross_after_authorization
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        evidence = caught.value.evidence
        # Local denial evidence only: the typed deadline refusal, with no
        # synthetic native receipt and no attempt-state rewrite.
        assert evidence.failure_kind is not None
        assert evidence.failure_kind.value == "deadline_exceeded"
        assert evidence.error == _DISPATCH_START_DEADLINE_ERROR
        assert evidence.attempt_receipt is None
        assert evidence.authority_receipt is None
        assert await store.count_receipts() == 0
        assert adapter.calls == 0
        assert clock.current == deadline
        item = await store.get_outbox_item(outbox_id)
        assert item is not None
        assert item.status == "in_progress"
        assert item.worker_id == "pipeline:clock"
        assert item.attempt_number == 1
        assert item.dispatch_source == "live"
    finally:
        await store.close()


async def test_plan_without_deadline_still_fences_on_the_event_ceiling(
    tmp_path, monkeypatch
):
    """The event content ceiling fences even when the plan carries no deadline."""
    store = SQLiteStorage(str(tmp_path / "fence-ceiling.db"))
    await store.initialize()
    try:
        ceiling = _future(120)
        event = _make_event()
        await store.admit_ingress(event, None, "live", content_deadline=ceiling)
        outbox_id = await _stage_owned_generation(store, event.event_id, snapshot=False)
        clock = _fenced_clock(monkeypatch, ceiling - timedelta(seconds=30))
        service, adapter = _fenced_service(store, event.event_id)
        route, plan = _make_route_and_plan()
        assert plan.deadline is None
        cross_after_authorization, _ = _cross_second_authorization(store, ceiling)
        monkeypatch.setattr(
            store, "authorize_native_submission", cross_after_authorization
        )
        with pytest.raises(_AdapterDeliveryError) as caught:
            await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
        assert adapter.calls == 0
        assert caught.value.error == _DISPATCH_START_DEADLINE_ERROR
        assert await store.count_receipts() == 0
        assert clock.current == ceiling
    finally:
        await store.close()


async def test_preflight_stall_before_the_bound_does_not_fence_early(
    tmp_path, monkeypatch
):
    """Slow rendering that stays inside the window still hands off."""
    store = SQLiteStorage(str(tmp_path / "fence-stall.db"))
    await store.initialize()
    try:
        deadline = _future(600)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        outbox_id = await _stage_owned_generation(
            store, event.event_id, plan_deadline=deadline
        )
        clock = _fenced_clock(monkeypatch, deadline - timedelta(seconds=120))

        class SlowRenderer(_FakeRenderingPipeline):
            async def render(self, *args, **kwargs):
                # Rendering stalls for a while but finishes before the bound.
                clock.current += timedelta(seconds=60)
                return await super().render(*args, **kwargs)

        adapter = _RecordingAdapter()
        service, _ = _make_service(
            adapters={"test_adapter": adapter},
            storage=store,
            rendering_pipeline=SlowRenderer(
                result=RenderingResult(
                    event_id=event.event_id,
                    target_adapter="test_adapter",
                    target_channel=None,
                    payload={"text": "hello"},
                )
            ),
        )
        route, plan = _make_route_and_plan()
        plan = replace(plan, deadline=deadline)
        receipt = (
    await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:clock",
            )
).primary_receipt
        assert receipt.status == "sent"
        assert adapter.calls == 1
        assert clock.current < deadline
    finally:
        await store.close()


async def test_retry_reconstruction_restored_deadline_still_fences(
    tmp_path, monkeypatch
):
    """The restart path fences on the restored frozen deadline, no receipts."""
    store = SQLiteStorage(str(tmp_path / "fence-restart.db"))
    await store.initialize()
    try:
        deadline = _future(120)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        # Historical row: frozen context rides inline metadata, no snapshot.
        item = DeliveryOutboxItem(
            outbox_id="obox-restart",
            event_id=event.event_id,
            delivery_plan_id="plan-001",
            route_id="route-001",
            target_adapter="test_adapter",
            attempt_number=1,
            status="in_progress",
            worker_id="pipeline:retry",
            lease_until=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
            metadata={
                "capability_level": "native",
                "delivery_strategy": "direct",
                "capability_field": None,
                "capability_reason": None,
                "deadline": deadline.isoformat(),
            },
        )
        await store.create_outbox_item(item)
        # Storage derives the same frozen bound from the historical bytes.
        bound = await store.observe_effective_dispatch_bound(
            event.event_id, "obox-restart"
        )
        assert bound is not None
        assert bound.source == "frozen_plan_deadline"
        assert bound.deadline == deadline
        # Retry reconstruction restores the deadline into the plan.
        reconstructed = reconstruct_retry_delivery_plan(
            item=await store.get_outbox_item("obox-restart"),
            previous_receipt=None,
            default_max_attempts=3,
            dispatch_snapshot=None,
        )
        assert reconstructed.plan.deadline == deadline

        clock = _fenced_clock(monkeypatch, deadline - timedelta(seconds=30))
        service, adapter = _fenced_service(store, event.event_id)
        cross_after_authorization, _ = _cross_second_authorization(store, deadline)
        monkeypatch.setattr(
            store, "authorize_native_submission", cross_after_authorization
        )
        with pytest.raises(_AdapterDeliveryError):
            await service.deliver_execution(
                event,
                reconstructed.route,
                reconstructed.plan,
                outbox_id="obox-restart",
                reserved_attempt_number=1,
                expected_outbox_worker_id="pipeline:retry",
            )
        assert adapter.calls == 0
        assert await store.count_receipts() == 0
        assert clock.current == deadline
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# No await between the final check and the adapter invocation
# ---------------------------------------------------------------------------


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(target_delivery_module))


def _find_function(tree: ast.Module, name: str):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _executable_statements(function):
    """Return the function body without a leading docstring expression."""
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body.pop(0)
    return body


def test_no_await_between_clock_observation_and_adapter_invocation() -> None:
    """The fence and the invocation are adjacent synchronous statements."""
    tree = _module_tree()
    fence = _find_function(tree, "_dispatch_start_fenced_invoke")
    body = _executable_statements(fence)
    # Statement order: the clock observation opens the fence, a synchronous
    # refusal gate may raise, and the invocation closes it.
    assert isinstance(body[0], ast.Assign), "fence must open with the clock reading"
    clock_call = body[0].value
    assert isinstance(clock_call, ast.Call)
    assert isinstance(clock_call.func, ast.Attribute)
    assert clock_call.func.attr == "now"
    gate = body[1]
    assert isinstance(gate, ast.If)
    assert all(
        not isinstance(node, ast.Await) for node in ast.walk(gate)
    ), "the refusal gate must raise synchronously without awaiting"
    assert isinstance(body[-1], ast.Return)
    invoke = body[-1].value
    assert isinstance(invoke, ast.Await)
    assert isinstance(invoke.value, ast.Call)
    assert isinstance(invoke.value.func, ast.Name)
    assert invoke.value.func.id == "deliver_fn"
    # The invocation is the only await in the entire fence function, so no
    # await can intervene between the clock observation and the native call.
    awaits = [node for node in ast.walk(fence) if isinstance(node, ast.Await)]
    assert awaits == [invoke]
    # The direct dispatch call site actually routes through this fence with
    # the real deliver_fn and the carried bound.
    caller = _find_function(tree, "_deliver_to_target_scoped")
    call_sites = [
        node
        for node in ast.walk(caller)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_dispatch_start_fenced_invoke"
    ]
    assert len(call_sites) == 1
    argument_names = {arg.id for arg in call_sites[0].args if isinstance(arg, ast.Name)}
    assert {"deliver_fn", "dispatch_bound"} <= argument_names


async def test_fenced_invoke_refuses_at_equality_and_invokes_before(monkeypatch):
    """Unit precision on the >= comparison against the carried bound."""
    clock = _fenced_clock(monkeypatch, datetime.now(timezone.utc))
    bound_instant = clock.current + timedelta(seconds=30)
    bound = EffectiveDispatchBound(bound_instant, "event_content_ceiling")
    invoked: list[RenderingResult] = []

    async def deliver(rendering_result):
        invoked.append(rendering_result)
        return AdapterHandoffResult(native_message_id="accepted")

    rendering_result = RenderingResult(
        event_id="evt-001",
        target_adapter="test_adapter",
        target_channel=None,
        payload={"text": "hello"},
    )
    # Strictly before: the invocation proceeds.
    handoff = await _dispatch_start_fenced_invoke(deliver, rendering_result, bound)
    assert handoff.native_message_id == "accepted"
    assert len(invoked) == 1
    # At exact equality and after: refused without invoking.
    clock.current = bound_instant
    with pytest.raises(_DispatchStartDeadlineRefused):
        await _dispatch_start_fenced_invoke(deliver, rendering_result, bound)
    clock.current = bound_instant + timedelta(seconds=1)
    with pytest.raises(_DispatchStartDeadlineRefused):
        await _dispatch_start_fenced_invoke(deliver, rendering_result, bound)
    assert len(invoked) == 1
    # A dormant fence (no applicable bound) never refuses.
    clock.current = bound_instant + timedelta(seconds=2)
    handoff = await _dispatch_start_fenced_invoke(deliver, rendering_result, None)
    assert handoff.native_message_id == "accepted"
    assert len(invoked) == 2


# ---------------------------------------------------------------------------
# Packetized radio: per-packet recheck preserves the accepted prefix
# ---------------------------------------------------------------------------


async def test_radio_packet_start_recheck_refuses_after_accepted_prefix(
    tmp_path, monkeypatch
):
    """A refused radio tail preserves the prefix's checkpointed handoff facts."""
    store = SQLiteStorage(str(tmp_path / "fence-radio.db"))
    await store.initialize()
    try:
        deadline = _future(600)
        event = _make_event()
        await store.admit_ingress(event, None, "live")
        item = DeliveryOutboxItem(
            outbox_id="obox-radio-fence",
            event_id=event.event_id,
            delivery_plan_id="plan-001",
            route_id="route-001",
            target_adapter="test_adapter",
            target_channel="0",
            status="pending",
            attempt_number=0,
        )
        item = await store.create_outbox_item(item)
        claimed = await store.claim_due_outbox_items(
            datetime.now(timezone.utc).isoformat(), "radio-owner"
        )
        assert any(row.outbox_id == item.outbox_id for row in claimed)
        assert await store.reserve_outbox_attempt(item.outbox_id, "radio-owner", 0) == 1
        _Clock.current = deadline - timedelta(seconds=30)
        monkeypatch.setattr(radio_delivery_module, "datetime", _Clock)

        wire_plan = plan_radio_text(
            "café α " * 12, prefix="[North] ", max_text_bytes=40
        )
        assert len(wire_plan.parts) >= 2

        class RadioRenderer:
            async def render(self, event, target_adapter, target_channel=None, **_):
                return RenderingResult(
                    event_id=event.event_id,
                    target_adapter="test_adapter",
                    target_channel="0",
                    payload={
                        "text": wire_plan.parts[0].wire_text,
                        "channel_index": 0,
                    },
                    radio_plan=wire_plan,
                )

        class RadioAdapter:
            platform = "test_platform"
            supports_radio_multipart = True

            def __init__(self) -> None:
                self.native_starts = 0

            async def deliver(self, packet):
                await packet.before_native_send()
                self.native_starts += 1
                if self.native_starts == 1:
                    # The clock crosses the bound after the first packet was
                    # natively accepted.
                    _Clock.current = deadline
                return AdapterHandoffResult(
                    native_channel_id="0",
                    confirmation_level="local_transport",
                )

        adapter = RadioAdapter()
        service = target_delivery_module.TargetDeliveryService(
            adapters={"test_adapter": adapter},
            rendering_pipeline=RadioRenderer(),  # type: ignore[arg-type]
            storage=store,
            diagnostician=Diagnostician(),
            lifecycle=DeliveryLifecycleService(
                logger=logging.getLogger("test.fence.radio")
            ),
            logger=logging.getLogger("test.fence.radio"),
        )
        route = Route(
            id="route-001",
            source=RouteSource(
                adapter="src_adapter",
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter="test_adapter", channel="0")],
        )
        plan = DeliveryPlan(
            plan_id="plan-001",
            event_id=event.event_id,
            target=RouteTarget(adapter="test_adapter", channel="0"),
            primary_strategy=DeliveryStrategy(method="direct"),
            deadline=deadline,
        )
        with pytest.raises(_AdapterDeliveryError, match="radio handoff incomplete"):
            await service.deliver_execution(
                event,
                route,
                plan,
                outbox_id=item.outbox_id,
                reserved_attempt_number=1,
                expected_outbox_worker_id="radio-owner",
            )
        # Exactly one native packet start happened; the refused packet never
        # started, and the accepted prefix keeps its checkpointed handoff fact.
        # The whole-attempt failure receipt does not erase that native history.
        receipts = await store.list_receipts_for_event(event.event_id)
        assert len(receipts) == 1
        assert receipts[0].status == "failed"
        assert adapter.native_starts == 1
        checkpoint = await store.get_radio_attempt(item.outbox_id, 1)
        assert checkpoint.progress.parts[0].state == "accepted"
        assert checkpoint.progress.parts[0].handoff_id is not None
        assert all(part.state == "unsent" for part in checkpoint.progress.parts[1:])
    finally:
        await store.close()


def test_effective_dispatch_bound_normalizes_aware_instant_to_utc():
    """The immutable authority carrier stores a canonical UTC instant."""
    offset = timezone(timedelta(hours=-5))
    bound = EffectiveDispatchBound(
        datetime(2026, 11, 3, 7, 0, tzinfo=offset),
        "event_content_ceiling",
    )
    assert bound.deadline == datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    assert bound.deadline.tzinfo is timezone.utc


def test_effective_dispatch_bound_rejects_unknown_sources_and_naive_instants():
    with pytest.raises(ValueError, match="Unknown effective dispatch bound source"):
        EffectiveDispatchBound(_future(120), "unregistered")
    with pytest.raises(ValueError, match="timezone-aware"):
        EffectiveDispatchBound(datetime(2026, 11, 3, 12, 0), "event_content_ceiling")
