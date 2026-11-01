# medre-test-tier: compatibility
"""Typed admission routing facts separated from opaque event metadata.

A plain text event admitted through the production admission path carrying
only built-in routing facts MUST NOT leave the ``opaque_envelope`` blocker:
the routing namespace persists as typed ``event_routing_facts`` rows written
in the admission transaction, and ``canonical_events.metadata`` keeps only
the opaque namespaces. Arbitrary ``native``/``custom`` values continue to
block exactly as before. Production reads compose ``EventMetadata`` from the
typed facts row plus the opaque metadata object, so every verified consumer
(staging, loop prevention, replay narrowing, rendering attribution) keeps
working through the composed event object.

Preexisting rows are never transformed: their inline routing stays their own
stored truth, the typed row wins whenever it exists, and a non-``{}``
historical metadata value keeps ``opaque_envelope`` active for the row's
lifetime.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.engine.replay.routing import (
    _clean_routing_metadata,
    _filter_replay_loops,
)
from medre.core.events import EventMetadata, EventRelation
from medre.core.events.metadata import (
    NativeMetadata,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)
from medre.core.events.schema import VALID_RELATION_TYPES
from medre.core.planning.delivery_plan import DeliveryPlan, DeliveryStrategy
from medre.core.rendering.attribution import build_relay_attribution
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage.backend import EventFilter, StorageError
from medre.core.storage.sqlite._projections import AUTHORIZED_EVENT_COLUMNS
from medre.core.storage.sqlite.serde import (
    _deserialize_metadata,
    _serialize_metadata,
    routing_facts_op,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


@pytest.mark.parametrize("operation", ["append", "admit_ingress"])
@pytest.mark.parametrize(
    "routing",
    [
        RoutingMetadata(matched_routes="not-an-array"),
        RoutingMetadata(matched_routes=(1,)),
        RoutingMetadata(route_trace="not-an-array"),
        RoutingMetadata(fanout_group=3),
    ],
)
async def test_invalid_routing_facts_refuse_admission_atomically(
    temp_storage, operation, routing
):
    event = msgspec.structs.replace(
        make_storage_event(event_id="invalid-facts"),
        metadata=EventMetadata(routing=routing),
    )
    with pytest.raises(ValueError):
        if operation == "append":
            await temp_storage.append(event)
        else:
            await temp_storage.admit_ingress(event, None, "live")
    assert not await temp_storage.event_exists(event.event_id)


@pytest.mark.parametrize("column", ["matched_routes", "route_trace"])
@pytest.mark.parametrize("value", ['"scalar"', "{}", "null"])
async def test_routing_fact_columns_require_json_arrays(temp_storage, column, value):
    event = msgspec.structs.replace(
        make_storage_event(event_id="typed-facts"),
        metadata=EventMetadata(routing=RoutingMetadata(matched_routes=("r1",))),
    )
    await temp_storage.admit_ingress(event, None, "live")
    with pytest.raises(StorageError):
        # The identifier comes only from this test's closed parameter set.
        await temp_storage._write(
            f"UPDATE event_routing_facts SET {column}=? WHERE event_id=?",  # nosec B608
            (value, event.event_id),
        )


_BODY = "secret"


def _routed_event(event_id="routed-facts", metadata=None, payload=None, relations=()):
    if metadata is None:
        metadata = EventMetadata(
            routing=RoutingMetadata(
                matched_routes=("r1",), fanout_group=None, route_trace=("r1",)
            )
        )
    return msgspec.structs.replace(
        make_storage_event(
            event_id=event_id,
            payload={"body": _BODY} if payload is None else payload,
            relations=relations or None,
        ),
        metadata=metadata,
    )


async def _admit(store, event):
    return await store.admit_ingress(event, None, "live")


async def _complete_ingress(store, event_id):
    claims = await store.claim_ingress_work(worker_id="worker")
    assert [claim.event_id for claim in claims] == [event_id]
    assert await store.complete_ingress_work(event_id, worker_id="worker")


def _raw_metadata(store_sql_row) -> dict:
    return json.loads(store_sql_row["metadata"])


# ---------------------------------------------------------------------------
# Opaque envelope: empty vs absent body, plain routed events
# ---------------------------------------------------------------------------


async def test_plain_routed_event_never_enters_the_opaque_envelope_blocker(
    tmp_path,
):
    """A routed text event's metadata column serializes to ``{}``.

    The typed facts row carries the routing decision in the same admission
    transaction; the metadata column holds no namespace at all, so the
    wholesale ``opaque_envelope`` probe observes zero rows for the event.
    """
    store = SQLiteStorage(str(tmp_path / "plain.db"))
    await store.initialize()
    try:
        event = _routed_event()
        await _admit(store, event)
        row = await store._read_one(
            "SELECT metadata, payload FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert _raw_metadata(row) == {}
        assert _raw_metadata(row) == json.loads(row["payload"])
        assessment = await store.assess_event_content_release(event.event_id)
        assert "opaque_envelope" not in assessment["blockers"]
        assert "unsettled_mandatory_owners" in assessment["blockers"]
    finally:
        await store.close()


async def test_empty_and_absent_body_events_stay_unblocked_by_the_envelope(
    tmp_path,
):
    """Empty and absent bodies both keep the envelope probe at zero.

    An explicitly present empty body is not an absent body: the admission
    identity records presence either way, and neither case writes opaque
    envelope bytes when the metadata carries only routing facts.
    """
    store = SQLiteStorage(str(tmp_path / "bodies.db"))
    await store.initialize()
    try:
        empty = _routed_event("empty-body", payload={"body": ""})
        absent = msgspec.structs.replace(_routed_event("absent-body"), payload={})
        await _admit(store, empty)
        await _admit(store, absent)
        for event_id in (empty.event_id, absent.event_id):
            row = await store._read_one(
                "SELECT metadata FROM canonical_events WHERE event_id=?",
                (event_id,),
            )
            assert _raw_metadata(row) == {}
            assessment = await store.assess_event_content_release(event_id)
            assert "opaque_envelope" not in assessment["blockers"]
        assert await store.get_body_presence_at_admission(empty.event_id) is True
        assert await store.get_body_presence_at_admission(absent.event_id) is False
    finally:
        await store.close()


async def test_arbitrary_native_only_metadata_still_blocks(tmp_path):
    """A native namespace alone keeps the envelope blocker active.

    Permanently conservative: only the typed routing facts left the opaque
    envelope; every opaque namespace a source supplies keeps blocking for
    the row's lifetime.
    """
    store = SQLiteStorage(str(tmp_path / "native-only.db"))
    await store.initialize()
    try:
        event = _routed_event(
            metadata=EventMetadata(
                routing=RoutingMetadata(matched_routes=("r1",)),
                native=NativeMetadata(data={"raw": "provider-blob"}),
            )
        )
        await _admit(store, event)
        row = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert set(_raw_metadata(row)) == {"native"}
        assessment = await store.assess_event_content_release(event.event_id)
        assert "opaque_envelope" in assessment["blockers"]
    finally:
        await store.close()


async def test_event_without_routing_namespace_writes_no_facts_row(tmp_path):
    """Absence of a facts row is the fact that no decision was accepted."""
    store = SQLiteStorage(str(tmp_path / "no-routing.db"))
    await store.initialize()
    try:
        event = _routed_event(metadata=EventMetadata())
        await _admit(store, event)
        assert (
            await store._read_one(
                "SELECT 1 AS present FROM event_routing_facts WHERE event_id=?",
                (event.event_id,),
            )
        ) is None
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        assert loaded.metadata.routing is None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Duplicate admission and source completion
# ---------------------------------------------------------------------------


async def test_duplicate_admission_never_rewrites_facts_or_metadata(tmp_path):
    """Duplicate admission is idempotent for both routing surfaces."""
    store = SQLiteStorage(str(tmp_path / "duplicate.db"))
    await store.initialize()
    try:
        event = _routed_event()
        await _admit(store, event)
        before = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        result = await _admit(store, event)
        assert result.created is False
        after = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert _raw_metadata(after) == _raw_metadata(before) == {}
        facts = await store._read_all(
            "SELECT matched_routes, route_trace, fanout_group "
            "FROM event_routing_facts WHERE event_id=?",
            (event.event_id,),
        )
        assert len(facts) == 1
        assert json.loads(facts[0]["matched_routes"]) == ["r1"]
        assert json.loads(facts[0]["route_trace"]) == ["r1"]
        assert facts[0]["fanout_group"] is None
    finally:
        await store.close()


async def test_source_completion_releases_the_owner_and_keeps_facts(tmp_path):
    """Ingress completion releases the mandatory owner; facts persist."""
    store = SQLiteStorage(str(tmp_path / "completion.db"))
    await store.initialize()
    try:
        event = _routed_event()
        await _admit(store, event)
        staged_blockers = await store.assess_event_content_release(event.event_id)
        assert "unsettled_mandatory_owners" in staged_blockers["blockers"]
        await _complete_ingress(store, event.event_id)
        settled = await store.assess_event_content_release(event.event_id)
        assert "unsettled_mandatory_owners" not in settled["blockers"]
        assert "opaque_envelope" not in settled["blockers"]
        facts = await store._read_one(
            "SELECT matched_routes FROM event_routing_facts WHERE event_id=?",
            (event.event_id,),
        )
        assert facts is not None
    finally:
        await store.close()


async def test_replay_content_read_denied_after_source_settlement(tmp_path):
    """Replay borrows the live source hold; settled sources deny content.

    The typed facts composition changes no read authority: a replay owner
    read of body content after ingress completion is denied exactly as
    before, while the routing facts stay readable through metadata.
    """
    store = SQLiteStorage(str(tmp_path / "replay-denial.db"))
    await store.initialize()
    try:
        event = _routed_event()
        await _admit(store, event)
        live = await store.read_event_for_replay(
            event.event_id, now=datetime.now(timezone.utc)
        )
        assert live.status == "retained"
        await _complete_ingress(store, event.event_id)
        denied = await store.read_event_for_replay(
            event.event_id, now=datetime.now(timezone.utc)
        )
        assert denied.status == "denied"
        assert denied.event is None
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Composed reads serve every verified consumer
# ---------------------------------------------------------------------------


async def test_owner_scoped_ingress_read_composes_routing(tmp_path):
    """The authorized owner read returns the composed metadata object."""
    store = SQLiteStorage(str(tmp_path / "owner-read.db"))
    await store.initialize()
    try:
        metadata = EventMetadata(
            routing=RoutingMetadata(
                matched_routes=("r1", "r2"), fanout_group="grp", route_trace=("r1",)
            ),
            custom={"extension.note": "operator-private"},
        )
        event = _routed_event(metadata=metadata)
        await _admit(store, event)
        read = await store.read_event_for_ingress(event.event_id)
        assert read.status == "retained" and read.event is not None
        assert read.event.metadata.routing == metadata.routing
        # The opaque namespace stays the row's own persisted value.
        raw = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert set(_raw_metadata(raw)) == {"custom"}
    finally:
        await store.close()


async def test_metadata_only_lookup_composes_routing_without_opaque_namespaces(
    tmp_path,
):
    """Metadata-only inspection returns structure without granting content.

    The projection keeps its exact pre-change shape: only the routing
    namespace is surfaced, now coalesced from the typed facts row and the
    row's own inline value.
    """
    store = SQLiteStorage(str(tmp_path / "metadata-only.db"))
    await store.initialize()
    try:
        routing = RoutingMetadata(
            matched_routes=("r1",), fanout_group=None, route_trace=("r1", "r2")
        )
        event = _routed_event(
            metadata=EventMetadata(routing=routing, radio=RadioMetadata(snr=3.0))
        )
        await _admit(store, event)
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        assert loaded.metadata.routing == routing
        assert loaded.metadata.radio is None
        assert loaded.metadata.custom == {}
        assert loaded.payload == {}
        # query_metadata composes identically.
        queried = [
            event
            async for event in store.query_metadata(
                EventFilter(event_kinds=frozenset({"message.created"}), limit=10)
            )
        ]
        assert [e.metadata.routing for e in queried] == [routing]
    finally:
        await store.close()


async def test_reply_and_reaction_ancestry_stays_relation_complete(tmp_path):
    """Source-owned reply/reaction ancestry survives the composed reads.

    Metadata-only lookups reconstruct relations from ``event_relations``
    exactly as before; the routing-facts separation touches no relation
    surface.
    """
    store = SQLiteStorage(str(tmp_path / "ancestry.db"))
    await store.initialize()
    try:
        parent = _routed_event("ancestry-parent")
        await _admit(store, parent)
        reply = _routed_event(
            "ancestry-reply",
            relations=(
                EventRelation(
                    relation_type="reply",
                    target_event_id=parent.event_id,
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                ),
            ),
        )
        reaction = _routed_event(
            "ancestry-reaction",
            relations=(
                EventRelation(
                    relation_type="reaction",
                    target_event_id=parent.event_id,
                    target_native_ref=None,
                    key="thumbs_up",
                    fallback_text=None,
                ),
            ),
        )
        await _admit(store, reply)
        await _admit(store, reaction)
        for child in (reply, reaction):
            loaded = await store.get_metadata(child.event_id)
            assert loaded is not None and len(loaded.relations) == 1
            relation = loaded.relations[0]
            assert relation.relation_type in VALID_RELATION_TYPES
            assert relation.target_event_id == parent.event_id
            # Reaction keys are user text; metadata-only lookups never
            # return them.
            assert relation.key is None
    finally:
        await store.close()


async def test_staging_uses_the_composed_routing_facts(tmp_path):
    """Staging through a storage-loaded event freezes the composed routing.

    ``OutboxManager.create_for_delivery`` reads the accepted routing
    decision from the event object; a storage-loaded event carries it only
    through the composed read.
    """
    store = SQLiteStorage(str(tmp_path / "staging.db"))
    await store.initialize()
    try:
        routing = RoutingMetadata(
            matched_routes=("r1",), fanout_group=None, route_trace=("r1",)
        )
        event = _routed_event(metadata=EventMetadata(routing=routing))
        await _admit(store, event)
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        target = RouteTarget(adapter="fake_presentation", channel="destination")
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
            event_id=event.event_id,
            route_id=route.id,
            target=target,
            primary_strategy=DeliveryStrategy(method="direct"),
            capability_level="native",
        )
        manager = OutboxManager(store, DeliveryLifecycleService())
        context = await manager.create_for_delivery(
            loaded, route, plan, target, target.adapter, pending=True
        )
        stored = await store.get_outbox_item(context.outbox_id)
        assert stored is not None and stored.metadata is not None
        snapshot_routing = stored.metadata["dispatch_context"]["routing"]
        assert snapshot_routing["matched_routes"] == ["r1"]
        assert snapshot_routing["route_trace"] == ["r1"]
    finally:
        await store.close()


async def test_replay_route_narrowing_reads_composed_facts(tmp_path):
    """Replay narrowing filters the composed matched routes and trace."""
    store = SQLiteStorage(str(tmp_path / "replay-narrowing.db"))
    await store.initialize()
    try:
        routing = RoutingMetadata(
            matched_routes=("r1", "r2"), fanout_group=None, route_trace=("r1", "r2")
        )
        event = _routed_event(metadata=EventMetadata(routing=routing))
        await _admit(store, event)
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        cleaned = _clean_routing_metadata(loaded, {"r2"})
        assert cleaned.metadata.routing is not None
        assert cleaned.metadata.routing.matched_routes == ("r2",)
        assert cleaned.metadata.routing.route_trace == ("r2",)
    finally:
        await store.close()


async def test_replay_loop_filter_reads_composed_facts(tmp_path):
    """Replay loop prevention sees previously traversed composed routes."""
    store = SQLiteStorage(str(tmp_path / "replay-loops.db"))
    await store.initialize()
    try:
        routing = RoutingMetadata(
            matched_routes=("r1",), fanout_group=None, route_trace=("r1",)
        )
        event = _routed_event(metadata=EventMetadata(routing=routing))
        await _admit(store, event)
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None

        class _StubRoute:
            id = "r1"

        class _StubPlan:
            target = RouteTarget(adapter="fake_presentation", channel="destination")

        warnings, filtered = _filter_replay_loops(
            loaded,
            [(_StubRoute(), _StubPlan())],
            previous_routing=loaded.metadata.routing,
        )
        assert filtered == []
        assert warnings and "r1" in warnings[0]
    finally:
        await store.close()


async def test_rendering_attribution_reads_composed_route_trace(tmp_path):
    """Attribution resolves the route from the composed routing namespace."""
    store = SQLiteStorage(str(tmp_path / "attribution.db"))
    await store.initialize()
    try:
        routing = RoutingMetadata(
            matched_routes=("r1",), fanout_group=None, route_trace=("r1", "r9")
        )
        event = _routed_event(metadata=EventMetadata(routing=routing))
        await _admit(store, event)
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        assert build_relay_attribution(loaded).route_id == "r1"
    finally:
        await store.close()


async def test_composed_reads_survive_restart(tmp_path):
    """Typed facts persist across a storage restart; reads recompose."""
    path = str(tmp_path / "restart.db")
    store = SQLiteStorage(path)
    await store.initialize()
    routing = RoutingMetadata(
        matched_routes=("r1",), fanout_group="grp", route_trace=("r1",)
    )
    event = _routed_event(metadata=EventMetadata(routing=routing))
    await _admit(store, event)
    await store.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        loaded = await reopened.get_metadata(event.event_id)
        assert loaded is not None
        assert loaded.metadata.routing == routing
        owner = await reopened.read_event_for_ingress(event.event_id)
        assert owner.status == "retained" and owner.event is not None
        assert owner.event.metadata.routing == routing
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Historical rows: no transformation, conservative blockers, coalesced reads
# ---------------------------------------------------------------------------


async def test_historical_inline_routing_still_serves_consumers(tmp_path):
    """A row whose metadata embeds routing keeps its bytes and its blocker.

    Additive schema creation only: the preexisting metadata JSON is never
    rewritten. The modeled preexisting row carries no typed facts row, so
    its inline routing value remains the row's own stored truth, consumers
    still compose it, and the non-``{}`` value keeps ``opaque_envelope``
    active for the row's lifetime.
    """
    store = SQLiteStorage(str(tmp_path / "historical.db"))
    await store.initialize()
    try:
        event = _routed_event("historical-inline", metadata=EventMetadata())
        await _admit(store, event)
        assert (
            await store._read_one(
                "SELECT 1 AS present FROM event_routing_facts WHERE event_id=?",
                (event.event_id,),
            )
        ) is None
        inline = {
            "routing": {
                "matched_routes": ["r-old"],
                "fanout_group": None,
                "route_trace": ["r-old"],
            },
            "custom": {},
        }
        await store._write_rowcount(
            "UPDATE canonical_events SET metadata=? WHERE event_id=?",
            (json.dumps(inline), event.event_id),
        )
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        assert loaded.metadata.routing is not None
        assert loaded.metadata.routing.matched_routes == ("r-old",)
        # The historical bytes are unchanged and the row stays a blocker.
        row = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert _raw_metadata(row) == inline
        assessment = await store.assess_event_content_release(event.event_id)
        assert "opaque_envelope" in assessment["blockers"]
    finally:
        await store.close()


async def test_typed_facts_row_wins_over_stale_inline_value(tmp_path):
    """When both surfaces exist, the typed facts row is the read authority.

    A row that carries a typed facts row plus a stale inline value reads
    the typed decision; the inline bytes stay untouched.
    """
    store = SQLiteStorage(str(tmp_path / "typed-wins.db"))
    await store.initialize()
    try:
        event = _routed_event()
        await _admit(store, event)
        stale = {
            "routing": {
                "matched_routes": ["r-stale"],
                "fanout_group": None,
                "route_trace": ["r-stale"],
            }
        }
        await store._write_rowcount(
            "UPDATE canonical_events SET metadata=? WHERE event_id=?",
            (json.dumps(stale), event.event_id),
        )
        loaded = await store.get_metadata(event.event_id)
        assert loaded is not None
        assert loaded.metadata.routing is not None
        assert loaded.metadata.routing.matched_routes == ("r1",)
        row = await store._read_one(
            "SELECT metadata FROM canonical_events WHERE event_id=?",
            (event.event_id,),
        )
        assert _raw_metadata(row) == stale
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Semantic equivalence: composition round-trips the pre-change serialization
# ---------------------------------------------------------------------------

_EQUIVALENCE_FIXTURES = {
    "all-six": EventMetadata(
        transport=TransportMetadata(protocol="mqtt", substrate="wifi"),
        routing=RoutingMetadata(
            matched_routes=("r1", "r2"), fanout_group="grp", route_trace=("r1",)
        ),
        radio=RadioMetadata(snr=7.5, rssi=-90.0),
        telemetry=TelemetryMetadata(metrics={"temperature_c": 21.5}),
        native=NativeMetadata(data={"provider_field": "value"}),
        custom={"extension.note": "operator-private"},
    ),
    "routing-only": EventMetadata(
        routing=RoutingMetadata(matched_routes=("r1",), route_trace=("r1",))
    ),
    "routing-fanout-only": EventMetadata(routing=RoutingMetadata(fanout_group="grp")),
    "empty": EventMetadata(),
    "transport-all-defaults": EventMetadata(transport=TransportMetadata()),
    "telemetry-only": EventMetadata(
        telemetry=TelemetryMetadata(metrics={"counter": 0})
    ),
    "native-only": EventMetadata(native=NativeMetadata(data={})),
    "custom-only": EventMetadata(custom={"plugin.value": 1}),
}


def _serde_composition(metadata: EventMetadata) -> EventMetadata:
    """Compose the metadata exactly as admission and the reads do.

    Mirrors the persisted split: ``_serialize_metadata`` writes the opaque
    namespaces, ``routing_facts_op`` writes the typed row, and the owner
    read folds the composed routing namespace back into the opaque object.
    """
    event = _routed_event(metadata=metadata)
    op = routing_facts_op(event, "recorded")
    opaque = json.loads(_serialize_metadata(metadata))
    if op is not None:
        _, params = op
        opaque["routing"] = {
            "matched_routes": json.loads(params[1]),
            "fanout_group": params[2],
            "route_trace": json.loads(params[3]),
        }
    return _deserialize_metadata(json.dumps(opaque))


@pytest.mark.parametrize("shape", sorted(_EQUIVALENCE_FIXTURES))
async def test_composed_metadata_equals_the_pre_change_serialization(tmp_path, shape):
    """Typed facts + opaque column compose to exactly the admitted metadata.

    For every fixture shape the pre-change serialization (all six namespace
    keys inline) round-trips to the admitted value; the persisted split
    composes to the same value both through the serde pair and through the
    production owner-read projection.
    """
    metadata = _EQUIVALENCE_FIXTURES[shape]
    pre_change = msgspec.json.decode(msgspec.json.encode(metadata), type=EventMetadata)
    assert pre_change == metadata
    assert _serde_composition(metadata) == metadata

    store = SQLiteStorage(str(tmp_path / f"equivalence-{shape}.db"))
    await store.initialize()
    try:
        event = _routed_event(f"equivalence-{shape}", metadata=metadata)
        await _admit(store, event)
        row = await store._read_one(
            f"SELECT {AUTHORIZED_EVENT_COLUMNS} FROM canonical_events "  # nosec B608 - closed schema projection; all values use bound parameters.
            "WHERE event_id=?",
            (event.event_id,),
        )
        assert row is not None
        assert _deserialize_metadata(row["metadata"]) == metadata
    finally:
        await store.close()
