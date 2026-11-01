# medre-test-tier: compatibility
"""Reconstruction and receipt-fallback guards for frozen outbox work snapshots.

The frozen accepted work context for one outbox generation lives in the
privately owned ``delivery_outbox_snapshots`` table, not in
``delivery_outbox.metadata``. These tests pin, on real SQLite storage:

- the snapshot is written atomically with its outbox row and keyed by the
  exact outbox identity (outbox_id plus the committed creation generation);
- new rows carry empty ``delivery_outbox.metadata`` while historical rows
  keep their inline bytes as the frozen context;
- retirement happens only inside guarded terminal settlement (``sent``,
  ``cancelled``, ``dead_lettered``, ``abandoned`` and the receipt-backed
  finalizers), never for ``pending``/``in_progress``/``retry_wait``/
  ``queued``, and a lost guard or rolled-back transaction preserves the
  snapshot;
- reconstruction consumes the typed owner-scoped read and reproduces the
  pre-change wire payloads exactly, failing closed on missing, corrupted,
  or unknown-version snapshots;
- an unretired snapshot blocks release assessment; a retired one blocks
  nothing; legacy inline metadata keeps blocking through the existing
  outbox-family content probe.

See ``docs/spec/accepted-context-ownership.md`` sections 3.2, 3.3, 4.2,
and 4.3.
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import datetime

import msgspec
import pytest

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.pipeline.receipt_factory import (
    capability_receipt_fields,
)
from medre.core.engine.pipeline.retry_plan import (
    ReconstructedRetryPlan,
    capability_fallback_from_snapshot,
    reconstruct_retry_delivery_plan,
)
from medre.core.events import EventMetadata
from medre.core.events.metadata import RoutingMetadata
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    DeliveryStrategy,
    RetryPolicy,
    delivery_target_identity,
)
from medre.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)
from medre.core.storage.backend import (
    OUTBOX_DISPATCH_SNAPSHOT_VERSION,
    DeliveryOutboxItem,
    OutboxDispatchSnapshot,
    StorageError,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_NOW = "2026-10-31T00:00:00+00:00"

# docs/spec/accepted-context-ownership.md section 4.3: the staged snapshot
# document members, and section 4.2: the reduced direct-snapshot key sets.
STAGED_SNAPSHOT_KEYS = frozenset({"route", "plan", "routing"})
DESTINATION_DIRECT_SNAPSHOT_KEYS = frozenset(
    {
        "destination_kind",
        "destination_hash",
        "destination_name",
        "destination_metadata",
        "capability_level",
        "delivery_strategy",
        "capability_field",
        "capability_reason",
        "deadline",
    }
)
CHANNEL_DIRECT_SNAPSHOT_KEYS = frozenset(
    {
        "capability_level",
        "delivery_strategy",
        "capability_field",
        "capability_reason",
        "deadline",
    }
)


def _routed_text_event(event_id: str) -> object:
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id, payload={"body": "secret"}),
        metadata=EventMetadata(routing=RoutingMetadata(matched_routes=("r1",))),
    )


def _destination_target() -> RouteTarget:
    return RouteTarget(
        adapter="fake_presentation",
        channel=None,
        destination=RouteDestination(
            kind="mesh",
            destination_hash="hash-1",
            destination_name="Room One",
            metadata={"room_id": "!room:example.org"},
        ),
    )


def _channel_target() -> RouteTarget:
    return RouteTarget(adapter="fake_presentation", channel="destination")


def _route_and_plan(event_id: str, target: RouteTarget) -> tuple[Route, DeliveryPlan]:
    route = Route(
        id="r1",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[target],
    )
    plan = DeliveryPlan(
        plan_id="plan-1",
        event_id=event_id,
        route_id=route.id,
        target=target,
        primary_strategy=DeliveryStrategy(method="direct"),
        capability_level="native",
        capability_field="mesh.native",
        capability_reason="adapter advertises native send",
        deadline=None,
        target_identity=delivery_target_identity(target),
        retry_policy=RetryPolicy(max_attempts=4, backoff_base=3.0, jitter=True),
    )
    return route, plan


async def _admit(store: SQLiteStorage, event_id: str) -> object:
    event = _routed_text_event(event_id)
    await store.admit_ingress(event, None, "live")
    return event


async def _stage_pending(
    store: SQLiteStorage, event: object, target: RouteTarget
) -> str:
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=True
    )
    assert context.outbox_id is not None
    return context.outbox_id


async def _create_direct(
    store: SQLiteStorage, event: object, target: RouteTarget
) -> str:
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=False
    )
    assert context.outbox_id is not None
    return context.outbox_id


async def _read_snapshot(store: SQLiteStorage, outbox_id: str) -> object | None:
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    return await store.get_outbox_dispatch_context(
        outbox_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
    )


async def _snapshot_count(store: SQLiteStorage) -> int:
    row = await store._read_one("SELECT COUNT(*) AS n FROM delivery_outbox_snapshots")
    assert row is not None
    return int(row["n"])


async def _claim(store: SQLiteStorage, outbox_id: str, worker: str = "delivery"):
    claimed = await store.claim_due_outbox_items(_NOW, worker)
    assert [item.outbox_id for item in claimed] == [outbox_id]
    return claimed[0]


def _item_identity(item: DeliveryOutboxItem) -> dict[str, object]:
    return {
        "event_id": item.event_id,
        "delivery_plan_id": item.delivery_plan_id,
        "target_adapter": item.target_adapter,
        "target_channel": item.target_channel,
    }


# ---------------------------------------------------------------------------
# Reconstruction from the frozen snapshot
# ---------------------------------------------------------------------------


def _assert_equivalent_context(
    reconstructed: ReconstructedRetryPlan,
    route: Route,
    plan: DeliveryPlan,
) -> None:
    """Compare the reconstructed wire payloads against the accepted ones."""
    assert msgspec.json.encode(reconstructed.plan) == msgspec.json.encode(plan)
    assert msgspec.json.encode(reconstructed.route) == msgspec.json.encode(route)


async def test_staged_reconstruction_reproduces_the_accepted_wire_payload(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """The frozen staged plan round-trips byte-for-byte through the snapshot."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-wire-staged")
    target = _destination_target()
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=True
    )
    assert context.outbox_id is not None
    item = await store.get_outbox_item(context.outbox_id)
    assert item is not None
    snapshot = await _read_snapshot(store, context.outbox_id)
    assert snapshot is not None
    reconstructed = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot,
    )
    _assert_equivalent_context(reconstructed, route, plan)
    assert reconstructed.routing is not None
    assert reconstructed.routing.matched_routes == ("r1",)
    assert reconstructed.retry_policy.max_attempts == 4


async def test_retry_after_restart_ignores_mutated_route_config(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Reconstruction reads the frozen snapshot, never current configuration."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-mutated-config")
    target = _destination_target()
    route, plan = _route_and_plan(event.event_id, target)
    outbox_id = await _stage_pending(store, event, target)
    await _claim(store, outbox_id)
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    snapshot = await _read_snapshot(store, outbox_id)
    assert snapshot is not None

    # A "current configuration" replanning produces a different decision; the
    # frozen context must win regardless of what a fresh planner would emit.
    current_plan = dataclass_replace(
        plan,
        capability_level="fallback",
        primary_strategy=DeliveryStrategy(method="fallback_text"),
        capability_reason="current router says text fallback",
    )
    assert current_plan.capability_level != plan.capability_level
    reconstructed = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot,
    )
    assert reconstructed.plan.capability_level == "native"
    assert reconstructed.plan.primary_strategy.method == "direct"
    assert reconstructed.plan.capability_reason == "adapter advertises native send"
    _assert_equivalent_context(reconstructed, route, plan)


async def test_overdue_deadline_is_restored_from_the_frozen_plan(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """An expired frozen deadline is restored exactly, then retires normally."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-overdue")
    target = _channel_target()
    route, plan = _route_and_plan(event.event_id, target)
    overdue = dataclass_replace(
        plan,
        deadline=datetime.fromisoformat("2026-10-30T00:00:00+00:00"),
    )
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, overdue, target, target.adapter, pending=True
    )
    assert context.outbox_id is not None
    item = await store.get_outbox_item(context.outbox_id)
    assert item is not None
    snapshot = await _read_snapshot(store, context.outbox_id)
    assert snapshot is not None
    reconstructed = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot,
    )
    assert reconstructed.plan.deadline is not None
    assert reconstructed.plan.deadline.isoformat() == "2026-10-30T00:00:00+00:00"
    _assert_equivalent_context(reconstructed, route, overdue)

    await _claim(store, context.outbox_id)
    assert await store.mark_outbox_dead_lettered(
        context.outbox_id, failure_kind="deadline_exceeded"
    )
    assert await _read_snapshot(store, context.outbox_id) is None


async def test_direct_snapshot_reconstruction_matches_the_inline_baseline(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Direct reconstruction from a snapshot equals the pre-change baseline."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-baseline")
    target = _destination_target()
    outbox_id = await _create_direct(store, event, target)
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    snapshot = await _read_snapshot(store, outbox_id)
    assert snapshot is not None
    from_snapshot = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
        dispatch_snapshot=snapshot,
    )

    legacy_metadata = dict(snapshot.context)
    legacy_item = dataclass_replace(item, metadata=legacy_metadata)
    from_inline = reconstruct_retry_delivery_plan(
        item=legacy_item,
        previous_receipt=None,
        default_max_attempts=3,
    )
    assert msgspec.json.encode(from_snapshot.plan) == msgspec.json.encode(
        from_inline.plan
    )
    assert msgspec.json.encode(from_snapshot.route) == msgspec.json.encode(
        from_inline.route
    )
    assert from_snapshot.routing is None and from_inline.routing is None


async def test_legacy_row_reconstruction_still_works_and_still_blocks(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """Historical inline bytes remain the frozen context and keep blocking."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-legacy")
    target = _destination_target()
    route, plan = _route_and_plan(event.event_id, target)
    legacy_item = DeliveryOutboxItem(
        outbox_id="obox-legacy-row",
        event_id=event.event_id,
        route_id=route.id,
        delivery_plan_id=plan.plan_id,
        target_adapter=target.adapter,
        target_channel=target.channel,
        target_address=target.destination.destination_hash,
        attempt_number=1,
        status="in_progress",
        metadata={
            "destination_kind": "mesh",
            "destination_hash": "hash-1",
            "destination_name": "Room One",
            "destination_metadata": {"room_id": "!room:example.org"},
            "capability_level": "native",
            "delivery_strategy": "direct",
            "capability_field": "mesh.native",
            "capability_reason": "adapter advertises native send",
            "deadline": None,
        },
    )
    await store.create_outbox_item(legacy_item)
    item = await store.get_outbox_item("obox-legacy-row")
    assert item is not None
    assert item.metadata
    reconstructed = reconstruct_retry_delivery_plan(
        item=item,
        previous_receipt=None,
        default_max_attempts=3,
    )
    assert reconstructed.plan.capability_level == "native"
    assert reconstructed.plan.target.destination is not None
    assert reconstructed.plan.target.destination.destination_name == "Room One"

    assessment = await store.assess_event_content_release("evt-legacy")
    assert "outbox_records" in assessment["blockers"]
    assert "unretired_outbox_snapshots" not in assessment["blockers"]
    row = await store._read_one(
        "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
        ("obox-legacy-row",),
    )
    assert row is not None and int(row["n"]) == 0


async def test_postchange_row_without_snapshot_fails_closed(
    outbox_temp_storage: SQLiteStorage,
) -> None:
    """A snapshot-less post-change row never falls back to re-planning."""
    store = outbox_temp_storage
    event = await _admit(store, "evt-missing-snapshot")
    outbox_id = await _create_direct(store, event, _channel_target())
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    with pytest.raises(ValueError, match="no frozen dispatch context"):
        reconstruct_retry_delivery_plan(
            item=item,
            previous_receipt=None,
            default_max_attempts=3,
        )
    with pytest.raises(ValueError, match="never-dispatched work"):
        staged = dataclass_replace(item, attempt_number=0)
        reconstruct_retry_delivery_plan(
            item=staged,
            previous_receipt=None,
            default_max_attempts=3,
        )


# ---------------------------------------------------------------------------
# Receipt capability fallback from the frozen snapshot
# ---------------------------------------------------------------------------


def test_capability_fallback_from_direct_snapshot() -> None:
    """A direct snapshot passes the frozen decision mapping through."""
    frozen = {
        "capability_level": "native",
        "delivery_strategy": "direct",
        "capability_field": "mesh.native",
        "capability_reason": "adapter advertises native send",
        "deadline": None,
    }
    snapshot = _snapshot(kind="direct", context=dict(frozen))
    # The sanitizer reads the decision keys by name and ignores the rest.
    assert capability_fallback_from_snapshot(snapshot) == frozen


def test_capability_fallback_from_staged_snapshot_reads_the_plan() -> None:
    """A staged snapshot reads the decision facts out of the retained plan."""
    snapshot = _snapshot(
        kind="staged",
        context={
            "route": {"id": "r1"},
            "plan": {
                "capability_level": "fallback",
                "primary_strategy": {"method": "fallback_text"},
                "capability_field": "text.body",
                "capability_reason": "no native send",
            },
            "routing": {"matched_routes": ["r1"]},
        },
    )
    assert capability_fallback_from_snapshot(snapshot) == {
        "capability_level": "fallback",
        "delivery_strategy": "fallback_text",
        "capability_field": "text.body",
        "capability_reason": "no native send",
    }


def test_capability_fallback_fields_match_the_receipt_sanitizer() -> None:
    """The composed fallback feeds capability_receipt_fields unchanged."""
    snapshot = _snapshot(
        kind="direct",
        context={
            "capability_level": "native",
            "delivery_strategy": "direct",
            "capability_field": "mesh.native",
            "capability_reason": "adapter advertises native send",
            "deadline": None,
        },
    )
    fields = capability_receipt_fields(
        metadata=capability_fallback_from_snapshot(snapshot)
    )
    assert fields == {
        "capability_level": "native",
        "capability_field": "mesh.native",
        "capability_reason": "adapter advertises native send",
        "delivery_strategy": "direct",
    }


def _snapshot(kind: str, context: dict) -> OutboxDispatchSnapshot:
    """Build a composed frozen-snapshot read result for fallback unit tests."""
    return OutboxDispatchSnapshot(
        outbox_id="obox-fallback",
        attempt_number=1,
        snapshot_version=OUTBOX_DISPATCH_SNAPSHOT_VERSION,
        kind=kind,
        context=context,
    )


async def test_receipt_capability_fallback_prefers_legacy_inline_metadata() -> None:
    """Historical evidence requires no new snapshot read after terminal work."""
    from unittest.mock import AsyncMock, Mock

    from medre.core.engine.pipeline.retry_plan import resolve_capability_fallback

    item = DeliveryOutboxItem(
        outbox_id="legacy",
        event_id="evt",
        route_id="route",
        delivery_plan_id="plan",
        target_adapter="target",
        target_channel="room",
        metadata={"capability_level": "fallback"},
    )
    storage = Mock(get_outbox_dispatch_context=AsyncMock())
    result = await resolve_capability_fallback(storage, item, Mock())
    assert result == {"capability_level": "fallback"}
    storage.get_outbox_dispatch_context.assert_not_awaited()


async def test_receipt_capability_fallback_uses_exact_snapshot_owner() -> None:
    """A receipt without inline fields uses the exact frozen owner identity."""
    from unittest.mock import AsyncMock, Mock

    from medre.core.engine.pipeline.retry_plan import resolve_capability_fallback

    item = DeliveryOutboxItem(
        outbox_id="owned",
        event_id="evt",
        route_id="route",
        delivery_plan_id="plan",
        target_adapter="target",
        target_channel="room",
        metadata={},
    )
    snapshot = OutboxDispatchSnapshot(
        outbox_id="owned",
        attempt_number=0,
        snapshot_version=OUTBOX_DISPATCH_SNAPSHOT_VERSION,
        kind="direct",
        context={"capability_level": "native"},
    )
    storage = Mock(get_outbox_dispatch_context=AsyncMock(return_value=snapshot))
    result = await resolve_capability_fallback(storage, item, Mock())
    assert result == {"capability_level": "native"}
    storage.get_outbox_dispatch_context.assert_awaited_once_with(
        "owned",
        event_id="evt",
        delivery_plan_id="plan",
        target_adapter="target",
        target_channel="room",
    )


async def test_receipt_fallback_read_failure_logs_cause_without_losing_outcome() -> (
    None
):
    """A completed native outcome is recorded without optional capability facts."""
    from unittest.mock import AsyncMock, Mock

    from medre.core.engine.pipeline.retry_plan import resolve_capability_fallback

    item = DeliveryOutboxItem(
        outbox_id="owned",
        event_id="evt",
        route_id="route",
        delivery_plan_id="plan",
        target_adapter="target",
        target_channel=None,
        metadata={},
    )
    storage = Mock(
        get_outbox_dispatch_context=AsyncMock(
            side_effect=StorageError("database locked")
        )
    )
    logger = Mock()
    assert await resolve_capability_fallback(storage, item, logger) is None
    logger.warning.assert_called_once()
    assert logger.warning.call_args.kwargs["exc_info"] is True
