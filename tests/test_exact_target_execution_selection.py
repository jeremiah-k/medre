"""Shared exact-target execution selection: request contract and delivery admission.

Pins the ``ReplayRequest.exact_target_selections`` contract (complete
identities with the preview's explicit None-vs-empty channel discipline,
duplicate refusal) and the replay deliver-stage admission gate that skips
unselected targets, refuses drifted or ambiguous ones before a send is
staged, and resolves or refuses in agreement with
:func:`medre.core.observability.exact_target.preview_exact_target` for the
same storage state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.engine.replay.delivery import (
    _EXACT_TARGET_REFUSAL_CATEGORIES,
    _ExactTargetRefusal,
    _plan_delivery_identity,
)
from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent
from medre.core.observability.exact_target import (
    ExactTargetSelection,
    preview_exact_target,
)
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.planning.delivery_plan import (
    stable_delivery_plan_id as _stable_plan_id,
)
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.backend import (
    OutboxDispatchSnapshotWrite,
    StorageError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import StubPipeline, make_engine
from tests.helpers.storage import make_storage_event
from tests.helpers.storage_outbox import (
    admit_event,
    create_outbox_item_with_parent,
    make_outbox_item,
)

_EVENT = "evt-exact-exec"
_ADAPTER = "fake_presentation"
_CHANNEL = "ch-0"
_SOURCE_ADAPTER = "fake_transport"
_SOURCE_CHANNEL = "ch-0"

_REFUSAL_REASON_PREFIX = "exact_target_refused: "


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _selection(
    event_id: str = _EVENT,
    plan: str | None = None,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
) -> ExactTargetSelection:
    return ExactTargetSelection(
        event_id=event_id,
        delivery_plan_id=plan if plan is not None else _plan_id(adapter, channel),
        target_adapter=adapter,
        target_channel=channel,
    )


def _plan_id(
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
    event_id: str = _EVENT,
) -> str:
    """The deterministic plan ID the planning stage produces for a target."""
    return _stable_plan_id(
        event_id,
        RouteTarget(adapter=adapter, channel=channel),
    )


def _row(**overrides: Any) -> Any:
    return make_outbox_item(
        delivery_plan_id=overrides.pop("delivery_plan_id", _plan_id()),
        target_adapter=overrides.pop("target_adapter", _ADAPTER),
        target_channel=overrides.pop("target_channel", _CHANNEL),
        event_id=overrides.pop("event_id", _EVENT),
        **overrides,
    )


def _matching_context(plan_id: str, channel: str = _CHANNEL) -> dict[str, Any]:
    """A staged frozen document that agrees with a row of the given plan."""
    return {
        "route": {"id": "route-1"},
        "plan": {
            "plan_id": plan_id,
            "event_id": _EVENT,
            "route_id": "route-1",
            "target": {"adapter": _ADAPTER, "channel": channel},
        },
        "routing": {},
    }


def _contradicting_context(plan_id: str) -> dict[str, Any]:
    """A staged frozen document naming a different plan than the row."""
    context = _matching_context(plan_id)
    context["plan"] = dict(context["plan"], plan_id="plan-rebound")
    return context


def _bare_plan(
    plan_id: str = _plan_id(),
    event_id: str = _EVENT,
    adapter: str = _ADAPTER,
    channel: str | None = _CHANNEL,
) -> DeliveryPlan:
    return DeliveryPlan(
        plan_id=plan_id,
        event_id=event_id,
        target=RouteTarget(adapter=adapter, channel=channel),
        primary_strategy=DeliveryStrategy(method="direct"),
    )


class _RecordingPipeline(StubPipeline):
    """Stub pipeline that records the plans handed to delivery."""

    def __init__(self, router: Router) -> None:
        super().__init__(router=router)
        self.delivered_plans: list[list[Any]] = []

    async def deliver(self, event: CanonicalEvent, plans: list[Any]) -> list[Any]:
        self.delivered_plans.append(list(plans))
        return plans


def _route_and_pipeline(
    *targets: RouteTarget,
) -> tuple[Router, _RecordingPipeline]:
    router = Router(
        routes=[
            Route(
                id="exact-route",
                source=RouteSource(
                    adapter=_SOURCE_ADAPTER,
                    event_kinds=("message.created",),
                    channel=_SOURCE_CHANNEL,
                ),
                targets=list(targets),
            )
        ]
    )
    return router, _RecordingPipeline(router)


async def _ensure_event(temp_storage: SQLiteStorage) -> None:
    """Ensure the replayed canonical event exists with the routed source shape."""
    if await temp_storage.get(_EVENT) is None:
        await temp_storage.append(make_storage_event(event_id=_EVENT))


async def _run_deliver(
    temp_storage: SQLiteStorage,
    pipeline: _RecordingPipeline,
    request: ReplayRequest,
) -> ReplayResult:
    await _ensure_event(temp_storage)
    engine = make_engine(temp_storage, pipeline=pipeline)
    results = [result async for result in engine.replay(request)]
    deliver_results = [result for result in results if result.stage == "deliver"]
    assert len(deliver_results) == 1
    return deliver_results[0]


def _delivered_plan_ids(pipeline: _RecordingPipeline) -> list[str]:
    return [
        getattr(plan, "plan_id", "")
        for delivered in pipeline.delivered_plans
        for plan in delivered
    ]


# ---------------------------------------------------------------------------
# ReplayRequest selection contract
# ---------------------------------------------------------------------------


def test_default_request_leaves_target_selection_unrestricted() -> None:
    assert ReplayRequest().exact_target_selections == ()


def test_request_accepts_complete_selections_with_explicit_channel_decision() -> None:
    selections = (
        ExactTargetSelection(_EVENT, "plan-a", _ADAPTER, "!room-a"),
        ExactTargetSelection(_EVENT, "plan-b", _ADAPTER, None),
    )
    request = ReplayRequest(exact_target_selections=selections)
    assert request.exact_target_selections == selections
    assert request.exact_target_selections[0].identity == DeliveryIdentity(
        _EVENT, "plan-a", _ADAPTER, "!room-a"
    )
    assert request.exact_target_selections[1].identity == DeliveryIdentity(
        _EVENT, "plan-b", _ADAPTER, None
    )


def test_request_rejects_elements_that_are_not_exact_selections() -> None:
    with pytest.raises(ValueError, match="ExactTargetSelection"):
        ReplayRequest(
            exact_target_selections=(  # type: ignore[arg-type]
                ("evt-exact-exec", "plan-a", "fake_presentation", "ch-0"),
            )
        )


def test_request_rejects_repeated_delivery_identity() -> None:
    selection = _selection()
    repeated = ExactTargetSelection(
        _EVENT, selection.delivery_plan_id, _ADAPTER, _CHANNEL
    )
    with pytest.raises(ValueError, match="must not repeat one delivery identity"):
        ReplayRequest(exact_target_selections=(selection, repeated))


def test_selection_carries_the_preview_identity_discipline_unchanged() -> None:
    """The shared selection key keeps its construction-time refusals."""
    with pytest.raises(ValueError, match="channel decision"):
        ExactTargetSelection(_EVENT, "plan-a", _ADAPTER, "")
    for args in (
        ("", "plan-a", _ADAPTER, None),
        (_EVENT, "", _ADAPTER, None),
        (_EVENT, "plan-a", "", None),
    ):
        with pytest.raises(ValueError, match="requires event, plan and adapter"):
            ExactTargetSelection(*args)


# ---------------------------------------------------------------------------
# Plan identity extraction
# ---------------------------------------------------------------------------


def test_plan_identity_is_read_from_bare_plans_and_route_plan_tuples() -> None:
    plan = _bare_plan()
    assert _plan_delivery_identity(plan) == DeliveryIdentity(
        _EVENT, plan.plan_id, _ADAPTER, _CHANNEL
    )
    assert _plan_delivery_identity(("route-stub", plan)) == DeliveryIdentity(
        _EVENT, plan.plan_id, _ADAPTER, _CHANNEL
    )


def test_opaque_or_incomplete_plan_identity_is_not_a_selected_target() -> None:
    assert _plan_delivery_identity(object()) is None
    incomplete = _bare_plan(adapter="missing")
    object.__setattr__(  # simulate an adapter-less plan target
        incomplete,
        "target",
        RouteTarget(adapter=None, channel=_CHANNEL),
    )
    assert _plan_delivery_identity(incomplete) is None


def test_execution_refusal_categories_are_the_preview_selection_failures() -> None:
    assert _EXACT_TARGET_REFUSAL_CATEGORIES == frozenset(
        {"not_found", "ambiguous", "route_or_plan_changed"}
    )


def test_refusal_reason_string_carries_category_and_identity() -> None:
    refusal = _ExactTargetRefusal(_EVENT, "plan-a", _ADAPTER, None, "ambiguous")
    assert refusal.reason == (
        f"exact_target_refused: ambiguous "
        f"({_EVENT}, plan-a, {_ADAPTER}, no-channel)"
    )
    channelled = _ExactTargetRefusal(_EVENT, "plan-a", _ADAPTER, "!r", "not_found")
    assert channelled.reason.startswith("exact_target_refused: not_found (")
    assert channelled.to_evidence() == {
        "event_id": _EVENT,
        "delivery_plan_id": "plan-a",
        "target_adapter": _ADAPTER,
        "target_channel": "!r",
        "category": "not_found",
        "reason": channelled.reason,
    }


# ---------------------------------------------------------------------------
# Delivery admission through the replay engine
# ---------------------------------------------------------------------------


async def test_selected_target_with_pending_row_is_delivered(temp_storage) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(temp_storage, _row(delivery_plan_id=plan_id))
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "passed"
    assert _delivered_plan_ids(pipeline) == [plan_id]
    assert result.output is not None
    assert result.output["replay"] is True
    assert "exact_target_refusals" not in result.output


async def test_selected_target_without_persisted_row_is_refused_ambiguous(
    temp_storage,
) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error is not None
    assert result.error.startswith(f"{_REFUSAL_REASON_PREFIX}ambiguous (")
    assert _delivered_plan_ids(pipeline) == []
    refusals = result.output["exact_target_refusals"]
    assert len(refusals) == 1
    assert refusals[0]["category"] == "ambiguous"
    assert refusals[0]["delivery_plan_id"] == plan_id


@pytest.mark.parametrize(
    "selection",
    [
        _selection(channel="!other"),
        _selection(plan="plan-elsewhere"),
        _selection(adapter="other_adapter"),
        _selection(event_id="evt-elsewhere"),
    ],
)
async def test_unselected_plans_are_skipped_without_a_send(
    temp_storage, selection: ExactTargetSelection
) -> None:
    await _ensure_event(temp_storage)
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(selection,),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error == "No delivery plans matched exact_target_selections filter"
    assert _delivered_plan_ids(pipeline) == []
    assert result.output is not None
    assert result.output["exact_target_refusals"] == []


async def test_drifted_frozen_binding_refuses_the_send(temp_storage) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await temp_storage.create_outbox_item(
        _row(delivery_plan_id=plan_id),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context=_contradicting_context(plan_id),
        ),
    )
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error is not None
    assert result.error.startswith(f"{_REFUSAL_REASON_PREFIX}route_or_plan_changed (")
    assert _delivered_plan_ids(pipeline) == []
    refusal = result.output["exact_target_refusals"][0]
    assert refusal["category"] == "route_or_plan_changed"
    assert refusal["reason"] == result.error


async def test_contested_live_generations_refuse_the_send(temp_storage) -> None:
    from dataclasses import replace

    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(temp_storage, _row(delivery_plan_id=plan_id))
    replay = replace(
        _row(delivery_plan_id=plan_id, attempt_number=1), dispatch_source="replay"
    )
    second = await temp_storage.create_outbox_item(replay, allocate_new_generation=True)
    assert second.attempt_number == 2
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error is not None
    assert result.error.startswith(f"{_REFUSAL_REASON_PREFIX}ambiguous (")
    assert _delivered_plan_ids(pipeline) == []


async def test_uninterpretable_frozen_snapshot_refuses_the_send(temp_storage) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await temp_storage.create_outbox_item(
        _row(delivery_plan_id=plan_id),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context={"route": {}},
        ),
    )
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error is not None
    assert result.error.startswith(f"{_REFUSAL_REASON_PREFIX}ambiguous (")
    assert _delivered_plan_ids(pipeline) == []


async def test_selected_channel_never_widens_into_sibling_channels(
    temp_storage,
) -> None:
    sibling = RouteTarget(adapter=_ADAPTER, channel="ch-sibling")
    selected_plan = _plan_id(channel=_CHANNEL)
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id=selected_plan, target_channel=_CHANNEL)
    )
    await create_outbox_item_with_parent(
        temp_storage,
        _row(
            delivery_plan_id=_plan_id(channel="ch-sibling"),
            target_channel="ch-sibling",
        ),
    )
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL),
        sibling,
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=selected_plan),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "passed"
    assert _delivered_plan_ids(pipeline) == [selected_plan]
    assert "exact_target_refusals" not in result.output


async def test_partial_refusal_delivers_survivor_and_records_refusal_evidence(
    temp_storage,
) -> None:
    sibling = RouteTarget(adapter=_ADAPTER, channel="ch-sibling")
    selected_plan = _plan_id(channel=_CHANNEL)
    sibling_plan = _plan_id(channel="ch-sibling")
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id=selected_plan, target_channel=_CHANNEL)
    )
    await temp_storage.create_outbox_item(
        _row(delivery_plan_id=sibling_plan, target_channel="ch-sibling"),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context=_contradicting_context(sibling_plan),
        ),
    )
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL),
        sibling,
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(
            _selection(plan=selected_plan),
            _selection(plan=sibling_plan, channel="ch-sibling"),
        ),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "passed"
    assert _delivered_plan_ids(pipeline) == [selected_plan]
    refusals = result.output["exact_target_refusals"]
    assert len(refusals) == 1
    assert refusals[0]["category"] == "route_or_plan_changed"
    assert refusals[0]["target_channel"] == "ch-sibling"
    assert refusals[0]["reason"].startswith(_REFUSAL_REASON_PREFIX)


async def test_target_adapters_and_exact_selection_compose(temp_storage) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(temp_storage, _row(delivery_plan_id=plan_id))
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        target_adapters=["other_adapter"],
        exact_target_selections=(_selection(plan=plan_id),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error == "No delivery plans matched target_adapters filter"
    assert _delivered_plan_ids(pipeline) == []


async def test_dry_run_suppression_precedes_exact_selection(temp_storage) -> None:
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.DRY_RUN,
        exact_target_selections=(_selection(),),
    )

    result = await _run_deliver(temp_storage, pipeline, request)

    assert result.status == "skipped"
    assert result.error == "dry_run: delivery suppressed"
    assert _delivered_plan_ids(pipeline) == []


async def test_verification_storage_failure_propagates_and_sends_nothing(
    temp_storage, monkeypatch
) -> None:
    plan_id = _plan_id()
    await _ensure_event(temp_storage)
    await create_outbox_item_with_parent(temp_storage, _row(delivery_plan_id=plan_id))
    router, pipeline = _route_and_pipeline(
        RouteTarget(adapter=_ADAPTER, channel=_CHANNEL)
    )
    request = ReplayRequest(
        mode=ReplayMode.BEST_EFFORT,
        exact_target_selections=(_selection(plan=plan_id),),
    )

    async def failing_read(event_id: str, *, metadata_only: bool = False) -> list[Any]:
        raise StorageError("backend read failed")

    monkeypatch.setattr(
        temp_storage, "list_outbox_items_for_event", failing_read
    )
    engine = make_engine(temp_storage, pipeline=pipeline)
    results = [result async for result in engine.replay(request)]

    assert _delivered_plan_ids(pipeline) == []
    assert any(
        result.status == "error" and "backend read failed" in (result.error or "")
        for result in results
    )


# ---------------------------------------------------------------------------
# Preview/execution agreement on shared fixture states
# ---------------------------------------------------------------------------


async def _gate_decision(
    temp_storage: SQLiteStorage,
    selection: ExactTargetSelection,
) -> tuple[list[DeliveryPlan], list[_ExactTargetRefusal]]:
    """Run the execution admission gate alone against the current state."""
    engine = make_engine(temp_storage, pipeline=None)
    request = ReplayRequest(exact_target_selections=(selection,))
    plan = _bare_plan(
        plan_id=selection.delivery_plan_id,
        event_id=selection.event_id,
        adapter=selection.target_adapter,
        channel=selection.target_channel,
    )
    return await engine._admit_plans_by_exact_selection([plan], request)


async def _assert_agreement(
    temp_storage: SQLiteStorage,
    selection: ExactTargetSelection,
    *,
    expected_category: str,
) -> None:
    """Both surfaces resolve the same target, or both refuse, on one state."""
    preview = await preview_exact_target(temp_storage, selection)
    admitted, refusals = await _gate_decision(temp_storage, selection)
    assert preview.category == expected_category
    assert bool(admitted) != (preview.category in _EXACT_TARGET_REFUSAL_CATEGORIES)
    if refusals:
        assert len(refusals) == 1
        assert refusals[0].category == preview.category
    else:
        assert len(admitted) == 1


async def test_agreement_pending_row_resolves_on_both_surfaces(temp_storage) -> None:
    plan_id = _plan_id()
    row = await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id=plan_id)
    )
    await _assert_agreement(
        temp_storage, _selection(plan=plan_id), expected_category="pending"
    )
    assert row.attempt_number == 1


async def test_agreement_completed_row_admits_new_replay_work(temp_storage) -> None:
    plan_id = _plan_id()
    row = await create_outbox_item_with_parent(
        temp_storage, _row(delivery_plan_id=plan_id)
    )
    claimed = await temp_storage.claim_due_outbox_items(
        datetime.now(timezone.utc).isoformat(), "worker-1"
    )
    assert row.outbox_id in {item.outbox_id for item in claimed}
    assert await temp_storage.mark_outbox_sent(row.outbox_id)
    await _assert_agreement(
        temp_storage, _selection(plan=plan_id), expected_category="completed"
    )


async def test_agreement_missing_outbox_row_refuses_on_both_surfaces(
    temp_storage,
) -> None:
    await admit_event(temp_storage, _EVENT)
    await _assert_agreement(
        temp_storage, _selection(plan="plan-unstored"), expected_category="ambiguous"
    )


async def test_agreement_contested_generations_refuse_on_both_surfaces(
    temp_storage,
) -> None:
    from dataclasses import replace

    plan_id = _plan_id()
    await create_outbox_item_with_parent(temp_storage, _row(delivery_plan_id=plan_id))
    replay = replace(
        _row(delivery_plan_id=plan_id, attempt_number=1), dispatch_source="replay"
    )
    await temp_storage.create_outbox_item(replay, allocate_new_generation=True)
    await _assert_agreement(
        temp_storage, _selection(plan=plan_id), expected_category="ambiguous"
    )


async def test_agreement_contradicted_binding_refuses_on_both_surfaces(
    temp_storage,
) -> None:
    plan_id = _plan_id()
    await admit_event(temp_storage, _EVENT)
    await temp_storage.create_outbox_item(
        _row(delivery_plan_id=plan_id),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context=_contradicting_context(plan_id),
        ),
    )
    await _assert_agreement(
        temp_storage,
        _selection(plan=plan_id),
        expected_category="route_or_plan_changed",
    )


async def test_agreement_uninterpretable_snapshot_refuses_on_both_surfaces(
    temp_storage,
) -> None:
    plan_id = _plan_id()
    await admit_event(temp_storage, _EVENT)
    await temp_storage.create_outbox_item(
        _row(delivery_plan_id=plan_id),
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="staged",
            context={"route": {}},
        ),
    )
    await _assert_agreement(
        temp_storage, _selection(plan=plan_id), expected_category="ambiguous"
    )


async def test_agreement_absent_event_refuses_on_both_surfaces(temp_storage) -> None:
    await _assert_agreement(
        temp_storage, _selection(), expected_category="not_found"
    )


async def test_agreement_selects_one_channel_and_counts_sibling_on_both_surfaces(
    temp_storage,
) -> None:
    await create_outbox_item_with_parent(
        temp_storage,
        _row(
            delivery_plan_id=_plan_id(channel=_CHANNEL),
            target_channel=_CHANNEL,
        ),
    )
    await create_outbox_item_with_parent(
        temp_storage,
        _row(
            delivery_plan_id=_plan_id(channel="ch-sibling"),
            target_channel="ch-sibling",
        ),
    )
    selection = _selection()
    preview = await preview_exact_target(temp_storage, selection)
    admitted, _ = await _gate_decision(temp_storage, selection)
    assert preview.category == "pending"
    assert preview.target_channel == _CHANNEL
    assert preview.excluded_sibling_count == 1
    assert len(admitted) == 1
    assert admitted[0].plan_id == selection.delivery_plan_id
