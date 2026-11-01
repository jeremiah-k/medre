# medre-test-tier: compatibility
"""Guards for the accepted context ownership contract.

Pins the persisted JSON inventories of ``canonical_events.metadata`` and
``delivery_outbox.metadata`` against
``docs/spec/accepted-context-ownership.md``, asserts the
classification-registry equivalence that contract states, and reproduces the
production-path blocker baseline for a staged routed text event.

Routing facts persist as typed ``event_routing_facts`` rows written in the
admission transaction; the metadata column keeps only the opaque namespaces
for new admissions. The outbox row's own metadata is empty for rows created
after the frozen work snapshot table landed; the dispatch context lives in
the privately owned ``delivery_outbox_snapshots`` table and its key set is
pinned here. Historical rows keep their inline bytes and are never
transformed. The blocker-baseline tests name the remaining persisted gap
exactly; later changes in the ownership stack update those current-gap
expectations case-by-case, while the permanently conservative opaque-content
cases (arbitrary ``custom`` and ``native`` metadata, incoming excerpts,
radio plans, attachments, operator decisions) must never flip.
"""

from __future__ import annotations

import json

import msgspec

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.events import EventMetadata
from medre.core.events.metadata import (
    NativeMetadata,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.routing.models import (
    Route,
    RouteDestination,
    RouteSource,
    RouteTarget,
)
from medre.core.storage.classification import STORAGE_COLUMN_CLASSES, ColumnClass
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# docs/spec/accepted-context-ownership.md section 4.2: the outbox row's own
# metadata key inventory is empty for rows created after the frozen work
# snapshot table; section 4.3: the frozen snapshot documents carry the
# context instead. A staged snapshot document holds exactly the
# OutboxDispatchContext members; a direct snapshot document holds the
# reduced route-decision keys plus the destination keys when the target is
# destination-addressed.
EMPTY_OUTBOX_METADATA_KEYS = frozenset()
STAGED_SNAPSHOT_DOCUMENT_KEYS = frozenset({"route", "plan", "routing"})
DIRECT_DESTINATION_SNAPSHOT_KEYS = frozenset(
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

# docs/spec/accepted-context-ownership.md section 4.1: the persisted metadata
# key set for new admissions is exactly the opaque namespace inventory. The
# routing namespace never persists inline; routing facts are typed
# ``event_routing_facts`` rows.
OPAQUE_EVENT_METADATA_NAMESPACES = frozenset(
    {"transport", "radio", "telemetry", "native", "custom"}
)

_BODY = "secret"
_NOW = "2026-10-31T00:00:00+00:00"


def _routed_text_event(event_id="staged-context", metadata=None):
    if metadata is None:
        metadata = EventMetadata(routing=RoutingMetadata(matched_routes=("r1",)))
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id, payload={"body": _BODY}),
        metadata=metadata,
    )


def _channel_target():
    return RouteTarget(adapter="fake_presentation", channel="destination")


def _destination_target():
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


def _route_and_plan(event_id, target):
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
    )
    return route, plan


async def _stage_pending(store, event, target):
    route, plan = _route_and_plan(event.event_id, target)
    manager = OutboxManager(store, DeliveryLifecycleService())
    context = await manager.create_for_delivery(
        event, route, plan, target, target.adapter, pending=True
    )
    assert context.outbox_id is not None
    return context


async def _read_snapshot(store, outbox_id):
    item = await store.get_outbox_item(outbox_id)
    assert item is not None
    snapshot = await store.get_outbox_dispatch_context(
        outbox_id,
        event_id=item.event_id,
        delivery_plan_id=item.delivery_plan_id,
        target_adapter=item.target_adapter,
        target_channel=item.target_channel,
    )
    assert snapshot is not None
    return snapshot


async def _complete_ingress(store, event_id):
    claims = await store.claim_ingress_work(worker_id="worker")
    assert [claim.event_id for claim in claims] == [event_id]
    assert await store.complete_ingress_work(event_id, worker_id="worker")


async def _settle_outbox_sent(store, outbox_id):
    claimed = await store.claim_due_outbox_items(_NOW, "delivery")
    assert [item.outbox_id for item in claimed] == [outbox_id]
    assert await store.mark_outbox_sent(outbox_id)


# ---------------------------------------------------------------------------
# Inventory-pinning guards
# ---------------------------------------------------------------------------


async def test_staged_outbox_metadata_key_set_matches_the_contract_inventory(
    tmp_path,
):
    """The persisted outbox metadata is empty; the snapshot carries the context.

    Any key a future producer adds to either surface fails this guard until
    the spec inventory gains a reviewed row for it.
    """
    store = SQLiteStorage(str(tmp_path / "outbox-keys.db"))
    await store.initialize()
    try:
        event = _routed_text_event()
        await store.admit_ingress(event, None, "live")
        staged = await _stage_pending(store, event, _channel_target())
        stored = await store.get_outbox_item(staged.outbox_id)
        assert stored is not None
        assert set(stored.metadata or {}) == EMPTY_OUTBOX_METADATA_KEYS
        # Section 4.3: the staged snapshot document members are exactly
        # route, plan, routing.
        snapshot = await _read_snapshot(store, staged.outbox_id)
        assert snapshot.kind == "staged"
        assert set(snapshot.context) == STAGED_SNAPSHOT_DOCUMENT_KEYS
    finally:
        await store.close()


async def test_destination_staged_outbox_snapshot_keys_match_the_inventory(
    tmp_path,
):
    """Destination-addressed staging carries the full snapshot key set."""
    store = SQLiteStorage(str(tmp_path / "destination-keys.db"))
    await store.initialize()
    try:
        event = _routed_text_event(event_id="dest-context")
        await store.admit_ingress(event, None, "live")
        staged = await _stage_pending(store, event, _destination_target())
        stored = await store.get_outbox_item(staged.outbox_id)
        assert stored is not None
        assert set(stored.metadata or {}) == EMPTY_OUTBOX_METADATA_KEYS
        snapshot = await _read_snapshot(store, staged.outbox_id)
        assert snapshot.kind == "staged"
        assert set(snapshot.context) == STAGED_SNAPSHOT_DOCUMENT_KEYS
        # The destination opaque values are frozen inside the retained plan.
        assert (
            snapshot.context["plan"]["target"]["destination"]["destination_name"]
            == "Room One"
        )
    finally:
        await store.close()


async def test_destination_direct_outbox_snapshot_keys_match_the_inventory(tmp_path):
    """Direct delivery pins the reduced destination-addressed snapshot shape."""
    store = SQLiteStorage(str(tmp_path / "direct-destination-keys.db"))
    await store.initialize()
    try:
        event = _routed_text_event(event_id="direct-destination-context")
        await store.admit_ingress(event, None, "live")
        target = _destination_target()
        route, plan = _route_and_plan(event.event_id, target)
        context = await OutboxManager(
            store, DeliveryLifecycleService()
        ).create_for_delivery(event, route, plan, target, target.adapter, pending=False)
        assert context.outbox_id is not None
        item = await store.get_outbox_item(context.outbox_id)
        assert item is not None
        assert set(item.metadata or {}) == EMPTY_OUTBOX_METADATA_KEYS
        snapshot = await _read_snapshot(store, context.outbox_id)
        assert snapshot.kind == "direct"
        assert set(snapshot.context) == DIRECT_DESTINATION_SNAPSHOT_KEYS
    finally:
        await store.close()


async def test_persisted_event_metadata_key_set_matches_the_contract_inventory(
    tmp_path,
):
    """Admission persists exactly the opaque namespace key set.

    The fully-populated fixture keeps every opaque namespace it carried; the
    ``routing`` namespace persists only as typed ``event_routing_facts``
    rows. A routing-only event serializes to ``{}``, so no opaque namespace
    is written at all.
    """
    store = SQLiteStorage(str(tmp_path / "event-keys.db"))
    await store.initialize()
    try:
        metadata = EventMetadata(
            transport=TransportMetadata(protocol="mqtt", substrate="wifi"),
            routing=RoutingMetadata(matched_routes=("r1",), route_trace=("r1",)),
            radio=RadioMetadata(snr=7.5, rssi=-90.0),
            telemetry=TelemetryMetadata(metrics={"temperature_c": 21.5}),
            native=NativeMetadata(data={"provider_field": "value"}),
            custom={"extension.note": "operator-private"},
        )
        event = _routed_text_event(event_id="full-metadata", metadata=metadata)
        await store.admit_ingress(event, None, "live")
        row = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert row is not None
        persisted = json.loads(row["metadata"])
        assert set(persisted) == OPAQUE_EVENT_METADATA_NAMESPACES
        routing_only = _routed_text_event(event_id="routing-only")
        await store.admit_ingress(routing_only, None, "live")
        routing_row = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (routing_only.event_id,),
        )
        assert routing_row is not None
        assert json.loads(routing_row["metadata"]) == {}
        facts = await store._read_one(
            "SELECT matched_routes, route_trace FROM event_routing_facts "
            "WHERE event_id=?",
            (routing_only.event_id,),
        )
        assert facts is not None
        assert json.loads(facts["matched_routes"]) == ["r1"]
        assert json.loads(facts["route_trace"]) == []
    finally:
        await store.close()


def test_registry_designation_matches_the_contract_inventory():
    """Both metadata columns are independently owned content in the registry.

    The spec's opaque-content designation and the classification registry
    cannot drift apart: this equivalence is the contract's own guard.
    """
    assert (
        STORAGE_COLUMN_CLASSES["delivery_outbox"]["metadata"]
        is ColumnClass.INDEPENDENTLY_OWNED_CONTENT
    )
    assert (
        STORAGE_COLUMN_CLASSES["canonical_events"]["metadata"]
        is ColumnClass.INDEPENDENTLY_OWNED_CONTENT
    )


# ---------------------------------------------------------------------------
# Production-path blocker baseline (the pinned current gap)
# ---------------------------------------------------------------------------


async def test_staged_routed_text_event_blockers_match_the_current_gap(tmp_path):
    """At staged time the blocker set is exactly the pinned baseline.

    Current-gap baseline: routing facts no longer block as opaque content —
    they persist as typed ``event_routing_facts`` rows written in the
    admission transaction, and the plain routed event's metadata column
    serializes to ``{}``. The new outbox row's metadata is empty, so the
    outbox family content probe no longer fires; the unretired frozen
    snapshot blocks as live work state instead.
    """
    store = SQLiteStorage(str(tmp_path / "staged-baseline.db"))
    await store.initialize()
    try:
        event = _routed_text_event(event_id="staged-baseline")
        await store.admit_ingress(event, None, "live")
        staged = await _stage_pending(store, event, _channel_target())
        assessment = await store.assess_event_content_release(event.event_id)
        assert assessment["blockers"] == [
            "active_ingress",
            "active_outbox",
            "unsettled_mandatory_owners",
            "unretired_outbox_snapshots",
        ]
        # The outbox row carries no inline opaque bytes; the frozen work
        # snapshot is the persisted context the live blocker names.
        stored = await store.get_outbox_item(staged.outbox_id)
        assert stored is not None
        assert stored.metadata == {}
        snapshot = await _read_snapshot(store, staged.outbox_id)
        assert snapshot.kind == "staged"
    finally:
        await store.close()


async def test_settled_routed_text_event_residual_blockers_match_the_gap(tmp_path):
    """After real ingress completion and terminal settlement the residual
    blocker set is exactly the pinned baseline.

    Current-gap baseline: the canonical envelope no longer blocks — the
    routed event's metadata column serializes to ``{}`` and its routing
    facts persist as typed rows — and the snapshot retired with the guarded
    terminal settlement, so the outbox side no longer blocks either. No
    persisted surface of a settled ordinary routed text event blocks
    release.
    """
    store = SQLiteStorage(str(tmp_path / "settled-baseline.db"))
    await store.initialize()
    try:
        event = _routed_text_event(event_id="settled-baseline")
        await store.admit_ingress(event, None, "live")
        staged = await _stage_pending(store, event, _channel_target())
        await _complete_ingress(store, event.event_id)
        await _settle_outbox_sent(store, staged.outbox_id)
        assessment = await store.assess_event_content_release(event.event_id)
        assert assessment["blockers"] == []
        # Every separated surface stays pinned: the outbox row carries no
        # inline bytes, its frozen snapshot is retired (absent, not empty),
        # the settled event's metadata column is ``{}``, and its routing
        # facts remain typed rows, not opaque bytes.
        outbox_row = await store.get_outbox_item(staged.outbox_id)
        assert outbox_row is not None
        assert outbox_row.metadata == {}
        snapshot = await store.get_outbox_dispatch_context(
            staged.outbox_id,
            event_id=outbox_row.event_id,
            delivery_plan_id=outbox_row.delivery_plan_id,
            target_adapter=outbox_row.target_adapter,
            target_channel=outbox_row.target_channel,
        )
        assert snapshot is None
        raw_event = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert raw_event is not None
        assert json.loads(raw_event["metadata"]) == {}
        facts = await store._read_one(
            "SELECT matched_routes FROM event_routing_facts WHERE event_id=?",
            (event.event_id,),
        )
        assert facts is not None
        assert json.loads(facts["matched_routes"]) == ["r1"]
    finally:
        await store.close()


async def test_arbitrary_custom_and_native_metadata_stay_opaque_blockers(tmp_path):
    """Arbitrary custom/native metadata blocks collection after settlement.

    Permanently conservative: no change in the ownership stack may exempt
    free-form ``custom`` or ``native`` values; the same holds for incoming
    excerpts, radio plans, attachments, and operator decisions, which the
    collection guard pins as wholesale policy blockers. Only the typed
    routing facts separated from the opaque envelope and only the outbox
    work snapshot retired; every opaque namespace a source supplies keeps
    blocking exactly as before.
    """
    store = SQLiteStorage(str(tmp_path / "opaque-namespaces.db"))
    await store.initialize()
    try:
        metadata = EventMetadata(
            routing=RoutingMetadata(matched_routes=("r1",)),
            native=NativeMetadata(data={"raw": "provider-blob"}),
            custom={"extension.note": "operator-private"},
        )
        event = _routed_text_event(event_id="opaque-namespaces", metadata=metadata)
        await store.admit_ingress(event, None, "live")
        staged = await _stage_pending(store, event, _channel_target())
        await _complete_ingress(store, event.event_id)
        await _settle_outbox_sent(store, staged.outbox_id)
        assessment = await store.assess_event_content_release(event.event_id)
        assert assessment["blockers"] == ["opaque_envelope"]
        envelope = next(
            copy
            for copy in assessment["copies"]
            if copy["surface"] == "opaque_envelope"
        )
        assert envelope["classification"] == "requires_independent_policy"
    finally:
        await store.close()
