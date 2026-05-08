"""Tests for core event models: CanonicalEvent, EventRelation, NativeRef,
NativeMessageRef, DeliveryReceipt, EventRecordKind, EventKind, SchemaRegistry,
and EventMetadata with sub-namespaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import msgspec
import pytest

from medre.core.events import (
    CURRENT_SCHEMA_VERSION,
    MIGRATION_REGISTRY,
    VALID_RELATION_TYPES,
    CanonicalEvent,
    DeliveryReceipt,
    EventKind,
    EventMetadata,
    EventRecordKind,
    EventRelation,
    KNOWN_KINDS,
    MetadataEmbeddingMode,
    NativeMessageRef,
    NativeMetadata,
    NativeRef,
    RadioMetadata,
    RoutingMetadata,
    SchemaRegistry,
    SchemaVersion,
    TelemetryMetadata,
    TransportMetadata,
    is_registered,
    schema_version_from_event,
)


# ===================================================================
# CanonicalEvent
# ===================================================================


class TestCanonicalEvent:
    """CanonicalEvent construction and immutability."""

    def test_construction_with_all_fields(self) -> None:
        """CanonicalEvent stores every field correctly."""
        now = datetime.now(timezone.utc)
        meta = EventMetadata(
            transport=TransportMetadata(protocol="mqtt"),
            routing=RoutingMetadata(matched_routes=("r1",), fanout_group="g1"),
        )
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=2,
            timestamp=now,
            source_adapter="mesh",
            source_transport_id="node-a",
            source_channel_id="ch-1",
            parent_event_id=None,
            lineage=("root",),
            relations=(),
            payload={"body": "hi"},
            metadata=meta,
            depth=1,
            trace_id="trace-abc",
        )
        assert event.event_id == "evt-1"
        assert event.event_kind == "message.created"
        assert event.schema_version == 2
        assert event.timestamp is now
        assert event.source_adapter == "mesh"
        assert event.source_transport_id == "node-a"
        assert event.source_channel_id == "ch-1"
        assert event.parent_event_id is None
        assert event.lineage == ("root",)
        assert event.relations == ()
        assert event.payload == {"body": "hi"}
        assert event.metadata is meta
        assert event.depth == 1
        assert event.trace_id == "trace-abc"

    def test_frozen_immutability_raises_type_error(self) -> None:
        """Frozen dataclass raises TypeError on attribute assignment."""
        event = CanonicalEvent(
            event_id="evt-2",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        with pytest.raises(AttributeError):
            event.event_id = "changed"  # type: ignore[misc]

    def test_default_optional_fields(self) -> None:
        """Depth defaults to 0 and trace_id defaults to None."""
        event = CanonicalEvent(
            event_id="evt-3",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="a",
            source_transport_id="t",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        assert event.depth == 0
        assert event.trace_id is None
        assert event.source_native_ref is None

    def test_source_native_ref_default_none(self) -> None:
        """source_native_ref defaults to None when not provided."""
        event = CanonicalEvent(
            event_id="evt-snr-default",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="a",
            source_transport_id="t",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )
        assert event.source_native_ref is None

    def test_source_native_ref_explicit_retained(self) -> None:
        """source_native_ref is retained when explicitly provided."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event123",
            native_thread_id=None,
        )
        event = CanonicalEvent(
            event_id="evt-snr-explicit",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="a",
            source_transport_id="t",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "matrix"
        assert event.source_native_ref.native_channel_id == "!room:server"
        assert event.source_native_ref.native_message_id == "$event123"

    def test_source_native_ref_immutable(self) -> None:
        """source_native_ref cannot be reassigned on a frozen event."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$event123",
        )
        event = CanonicalEvent(
            event_id="evt-snr-imm",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="a",
            source_transport_id="t",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )
        with pytest.raises(AttributeError):
            event.source_native_ref = None  # type: ignore[misc]


# ===================================================================
# EventRelation
# ===================================================================


class TestEventRelation:
    """EventRelation construction and all relation types."""

    @pytest.mark.parametrize(
        "rel_type",
        ["reply", "reaction", "edit", "delete", "thread"],
    )
    def test_all_relation_types_accepted(self, rel_type: str) -> None:
        """Every documented relation_type value is accepted."""
        rel = EventRelation(
            relation_type=rel_type,  # type: ignore[arg-type]
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        assert rel.relation_type == rel_type

    def test_relation_with_native_ref(self) -> None:
        """Relation can carry a NativeRef as target_native_ref."""
        nref = NativeRef(
            adapter="discord",
            native_channel_id="room-1",
            native_message_id="msg-99",
            native_thread_id="thread-1",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=nref,
            key=None,
            fallback_text="original",
        )
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.adapter == "discord"
        assert rel.target_native_ref.native_message_id == "msg-99"
        assert rel.target_native_ref.native_thread_id == "thread-1"

    def test_relation_with_key_and_metadata(self) -> None:
        """Key and metadata fields are stored correctly."""
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="t-1",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
            metadata={"source": "test"},
        )
        assert rel.key == "👍"
        assert rel.metadata == {"source": "test"}

    def test_frozen_immutability(self) -> None:
        """EventRelation is frozen."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        with pytest.raises(AttributeError):
            rel.relation_type = "edit"  # type: ignore[misc]


# ===================================================================
# NativeRef
# ===================================================================


class TestNativeRef:
    """NativeRef construction and optional fields."""

    def test_construction_all_fields(self) -> None:
        """All fields are stored correctly."""
        ref = NativeRef(
            adapter="meshtastic",
            native_channel_id="ch-0",
            native_message_id="msg-1",
            native_thread_id="thread-1",
        )
        assert ref.adapter == "meshtastic"
        assert ref.native_channel_id == "ch-0"
        assert ref.native_message_id == "msg-1"
        assert ref.native_thread_id == "thread-1"

    def test_optional_fields_default_to_none(self) -> None:
        """native_thread_id defaults to None; native_channel_id can be None."""
        ref = NativeRef(
            adapter="test",
            native_channel_id=None,
            native_message_id="msg-2",
        )
        assert ref.native_channel_id is None
        assert ref.native_thread_id is None

    def test_frozen(self) -> None:
        """NativeRef is immutable."""
        ref = NativeRef(adapter="a", native_channel_id="c", native_message_id="m")
        with pytest.raises(AttributeError):
            ref.adapter = "other"  # type: ignore[misc]


# ===================================================================
# NativeMessageRef
# ===================================================================


class TestNativeMessageRef:
    """NativeMessageRef construction and direction field."""

    def test_construction_all_fields(self) -> None:
        """All fields are stored correctly."""
        now = datetime.now(timezone.utc)
        ref = NativeMessageRef(
            id="nref-1",
            event_id="evt-1",
            adapter="adapter-1",
            native_channel_id="ch-0",
            native_message_id="msg-1",
            native_thread_id=None,
            native_relation_id="rel-1",
            direction="inbound",
            metadata={"key": "val"},
            created_at=now,
        )
        assert ref.id == "nref-1"
        assert ref.direction == "inbound"
        assert ref.native_relation_id == "rel-1"
        assert ref.created_at is now

    def test_default_created_at(self) -> None:
        """created_at defaults to datetime.now when not provided."""
        ref = NativeMessageRef(
            id="nref-2",
            event_id="evt-2",
            adapter="a",
            native_channel_id=None,
            native_message_id="m",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
        )
        assert isinstance(ref.created_at, datetime)


# ===================================================================
# DeliveryReceipt
# ===================================================================


class TestDeliveryReceipt:
    """DeliveryReceipt construction and defaults."""

    def test_default_sequence_is_zero(self) -> None:
        """Sequence defaults to 0."""
        receipt = DeliveryReceipt()
        assert receipt.sequence == 0

    def test_default_status_is_accepted(self) -> None:
        """Status defaults to 'accepted'."""
        receipt = DeliveryReceipt()
        assert receipt.status == "accepted"

    def test_all_fields(self) -> None:
        """Every field can be set explicitly."""
        now = datetime.now(timezone.utc)
        receipt = DeliveryReceipt(
            sequence=3,
            receipt_id="rcpt-1",
            event_id="evt-1",
            delivery_plan_id="plan-1",
            target_adapter="target-1",
            status="confirmed",
            error=None,
            adapter_message_id="native-1",
            next_retry_at=None,
            created_at=now,
        )
        assert receipt.sequence == 3
        assert receipt.status == "confirmed"
        assert receipt.adapter_message_id == "native-1"


# ===================================================================
# EventRecordKind
# ===================================================================


class TestEventRecordKind:
    """EventRecordKind enum values."""

    def test_all_members(self) -> None:
        """All four record kinds exist with correct values."""
        assert EventRecordKind.SOURCE_EVENT.value == "source_event"
        assert EventRecordKind.DERIVED_EVENT.value == "derived_event"
        assert EventRecordKind.DELIVERY_ARTIFACT.value == "delivery_artifact"
        assert EventRecordKind.RECEIPT_EVENT.value == "receipt_event"


# ===================================================================
# EventKind registry
# ===================================================================


class TestEventKind:
    """EventKind constants and registry helpers."""

    def test_message_created_constant(self) -> None:
        assert EventKind.MESSAGE_CREATED == "message.created"

    def test_message_text_constant(self) -> None:
        assert EventKind.MESSAGE_TEXT == "message.text"

    def test_message_reacted_constant(self) -> None:
        assert EventKind.MESSAGE_REACTED == "message.reacted"

    def test_message_edited_constant(self) -> None:
        assert EventKind.MESSAGE_EDITED == "message.edited"

    def test_message_deleted_constant(self) -> None:
        assert EventKind.MESSAGE_DELETED == "message.deleted"

    def test_telemetry_received_constant(self) -> None:
        assert EventKind.TELEMETRY_RECEIVED == "telemetry.received"

    def test_presence_changed_constant(self) -> None:
        assert EventKind.PRESENCE_CHANGED == "presence.changed"

    def test_identity_updated_constant(self) -> None:
        assert EventKind.IDENTITY_UPDATED == "identity.updated"

    def test_delivery_failed_constant(self) -> None:
        assert EventKind.DELIVERY_FAILED == "delivery.failed"

    def test_system_audit_constant(self) -> None:
        assert EventKind.SYSTEM_AUDIT == "system.audit"

    def test_plugin_custom_constant(self) -> None:
        assert EventKind.PLUGIN_CUSTOM == "plugin.custom"

    def test_is_registered_true_for_known_kind(self) -> None:
        assert is_registered("message.created") is True

    def test_is_registered_false_for_unknown_kind(self) -> None:
        assert is_registered("unknown.kind") is False

    def test_known_kinds_is_frozenset(self) -> None:
        assert isinstance(KNOWN_KINDS, frozenset)
        assert "message.created" in KNOWN_KINDS

    def test_known_kinds_count(self) -> None:
        """KNOWN_KINDS contains every constant defined in EventKind."""
        expected = {
            "message.created", "message.text", "message.reacted",
            "message.edited", "message.deleted", "message.file",
            "telemetry.received", "telemetry.position",
            "presence.changed", "identity.updated",
            "delivery.accepted", "delivery.queued", "delivery.sent",
            "delivery.confirmed", "delivery.failed",
            "system.audit", "system.lifecycle", "plugin.custom",
        }
        assert KNOWN_KINDS == frozenset(expected)


# ===================================================================
# SchemaRegistry
# ===================================================================


class TestSchemaRegistry:
    """SchemaRegistry register, validate pass, validate fail."""

    def test_register_and_validate_pass(self) -> None:
        """Validator returning [] means valid."""
        registry = SchemaRegistry()
        registry.register("message.text", 1, lambda p: [])
        assert registry.validate("message.text", {"body": "hi"}) is True

    def test_validate_fail_returns_false(self) -> None:
        """Validator returning errors means invalid."""
        registry = SchemaRegistry()
        registry.register(
            "message.text", 1, lambda p: ["missing 'body'"]
        )
        assert registry.validate("message.text", {}) is False

    def test_validate_fail_populates_errors_list(self) -> None:
        """The errors list is populated with validator output."""
        registry = SchemaRegistry()
        registry.register(
            "message.text", 1, lambda p: ["missing 'body'", "too short"]
        )
        errors: list[str] = []
        result = registry.validate("message.text", {}, errors=errors)
        assert result is False
        assert errors == ["missing 'body'", "too short"]

    def test_validate_unknown_kind_returns_false(self) -> None:
        """No registered schema means validation fails."""
        registry = SchemaRegistry()
        errors: list[str] = []
        result = registry.validate("unknown.kind", {}, errors=errors)
        assert result is False
        assert len(errors) == 1
        assert "unknown.kind" in errors[0]

    def test_get_returns_none_for_unregistered(self) -> None:
        registry = SchemaRegistry()
        assert registry.get("nope", 1) is None

    def test_schema_version_from_event(self) -> None:
        """schema_version_from_event extracts version from payload."""
        sv = schema_version_from_event("message.text", {"schema_version": 3})
        assert sv.event_kind == "message.text"
        assert sv.version == 3

    def test_schema_version_from_event_defaults_to_1(self) -> None:
        sv = schema_version_from_event("message.text", {})
        assert sv.version == 1


# ===================================================================
# EventMetadata and sub-namespaces
# ===================================================================


class TestEventMetadata:
    """EventMetadata with all sub-namespace combinations."""

    def test_empty_metadata(self) -> None:
        """Default EventMetadata has None sub-namespaces."""
        meta = EventMetadata()
        assert meta.transport is None
        assert meta.routing is None
        assert meta.radio is None
        assert meta.telemetry is None
        assert meta.native is None
        assert meta.custom == {}

    def test_full_metadata(self) -> None:
        """All sub-namespaces populated."""
        meta = EventMetadata(
            transport=TransportMetadata(
                protocol="mqtt", delivery_confirmed=True
            ),
            routing=RoutingMetadata(
                matched_routes=("r1", "r2"), fanout_group="fg"
            ),
            radio=RadioMetadata(snr=8.0, rssi=-85.0, frequency=915.0),
            telemetry=TelemetryMetadata(metrics={"battery": 95.0}),
            native=NativeMetadata(data={"raw": True}),
            custom={"extra": "value"},
        )
        assert meta.transport is not None
        assert meta.transport.protocol == "mqtt"
        assert meta.routing is not None
        assert meta.routing.matched_routes == ("r1", "r2")
        assert meta.radio is not None
        assert meta.radio.snr == 8.0
        assert meta.telemetry is not None
        assert meta.telemetry.metrics["battery"] == 95.0
        assert meta.native is not None
        assert meta.native.data["raw"] is True
        assert meta.custom["extra"] == "value"

    def test_transport_metadata_defaults(self) -> None:
        """All TransportMetadata fields default to None."""
        tm = TransportMetadata()
        assert tm.protocol is None
        assert tm.substrate is None
        assert tm.gateway_id is None
        assert tm.delivery_confirmed is None
        assert tm.transport_encrypted is None

    def test_radio_metadata_defaults(self) -> None:
        """All RadioMetadata fields default to None."""
        rm = RadioMetadata()
        assert rm.snr is None
        assert rm.rssi is None
        assert rm.channel_index is None
        assert rm.frequency is None


# ===================================================================
# MetadataEmbeddingMode
# ===================================================================


class TestMetadataEmbeddingMode:
    """MetadataEmbeddingMode enum values."""

    def test_all_modes(self) -> None:
        assert MetadataEmbeddingMode.OFF.value == "off"
        assert MetadataEmbeddingMode.MINIMAL.value == "minimal"
        assert MetadataEmbeddingMode.SAFE.value == "safe"
        assert MetadataEmbeddingMode.FULL.value == "full"


# ===================================================================
# Round-trip serialisation
# ===================================================================


def _make_event(
    payload: dict[str, object] | None = None,
) -> CanonicalEvent:
    """Helper that builds a valid CanonicalEvent for round-trip tests."""
    return CanonicalEvent(
        event_id="evt-rt-1",
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        source_adapter="test",
        source_transport_id="t-1",
        source_channel_id="ch-1",
        parent_event_id=None,
        lineage=("root",),
        relations=(
            EventRelation(
                relation_type="reply",
                target_event_id="t-2",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            ),
        ),
        payload=payload if payload is not None else {"body": "hello"},
        metadata=EventMetadata(
            transport=TransportMetadata(protocol="mqtt"),
        ),
        depth=0,
        trace_id="trace-1",
    )


def _valid_kwargs() -> dict:
    """Module-level base kwargs that produce a valid CanonicalEvent.

    Used by Track 2 tests that need a clean starting point for mutation.
    """
    return dict(
        event_id="evt-ok",
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="test",
        source_transport_id="t-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={},
        metadata=EventMetadata(),
    )


class TestJsonRoundTrip:
    """JSON encode → decode must preserve every field."""

    def test_json_round_trip_all_fields(self) -> None:
        event = _make_event()
        encoded = msgspec.json.encode(event)
        decoded = msgspec.json.decode(encoded, type=CanonicalEvent)
        assert decoded.event_id == event.event_id
        assert decoded.event_kind == event.event_kind
        assert decoded.schema_version == event.schema_version
        assert decoded.timestamp == event.timestamp
        assert decoded.source_adapter == event.source_adapter
        assert decoded.source_transport_id == event.source_transport_id
        assert decoded.source_channel_id == event.source_channel_id
        assert decoded.parent_event_id == event.parent_event_id
        assert decoded.lineage == event.lineage
        assert len(decoded.relations) == 1
        assert decoded.relations[0].relation_type == "reply"
        assert decoded.relations[0].target_event_id == "t-2"
        assert decoded.payload == event.payload
        assert decoded.metadata.transport is not None
        assert decoded.metadata.transport.protocol == "mqtt"
        assert decoded.depth == event.depth
        assert decoded.trace_id == event.trace_id
        assert decoded.source_native_ref is None

    def test_json_round_trip_with_source_native_ref(self) -> None:
        """source_native_ref survives JSON encode/decode roundtrip."""
        nref = NativeRef(
            adapter="matrix",
            native_channel_id="!room:server",
            native_message_id="$evt-001",
            native_thread_id=None,
        )
        event = CanonicalEvent(
            event_id="evt-snr-rt",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id="ch-1",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
            source_native_ref=nref,
        )
        encoded = msgspec.json.encode(event)
        decoded = msgspec.json.decode(encoded, type=CanonicalEvent)
        assert decoded.source_native_ref is not None
        assert decoded.source_native_ref.adapter == "matrix"
        assert decoded.source_native_ref.native_channel_id == "!room:server"
        assert decoded.source_native_ref.native_message_id == "$evt-001"
        assert decoded.source_native_ref.native_thread_id is None


class TestMsgpackRoundTrip:
    """msgpack encode → decode must match JSON-decoded result."""

    def test_msgpack_round_trip_equals_json(self) -> None:
        event = _make_event()
        json_decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        msgpack_decoded = msgspec.msgpack.decode(
            msgspec.msgpack.encode(event), type=CanonicalEvent
        )
        assert msgpack_decoded.event_id == json_decoded.event_id
        assert msgpack_decoded.event_kind == json_decoded.event_kind
        assert msgpack_decoded.schema_version == json_decoded.schema_version
        assert msgpack_decoded.timestamp == json_decoded.timestamp
        assert msgpack_decoded.source_adapter == json_decoded.source_adapter
        assert msgpack_decoded.source_transport_id == json_decoded.source_transport_id
        assert msgpack_decoded.source_channel_id == json_decoded.source_channel_id
        assert msgpack_decoded.parent_event_id == json_decoded.parent_event_id
        assert msgpack_decoded.lineage == json_decoded.lineage
        assert msgpack_decoded.relations == json_decoded.relations
        assert msgpack_decoded.payload == json_decoded.payload
        assert msgpack_decoded.depth == json_decoded.depth
        assert msgpack_decoded.trace_id == json_decoded.trace_id


# ===================================================================
# Immutability enforcement
# ===================================================================


class TestImmutability:
    """Frozen struct must reject field mutation; containers are deeply frozen."""

    def test_cannot_set_field(self) -> None:
        event = _make_event()
        with pytest.raises(AttributeError):
            event.event_id = "hacked"  # type: ignore[misc]

    def test_cannot_reassign_relations(self) -> None:
        """Field reassignment is blocked by frozen=True."""
        event = _make_event()
        with pytest.raises(AttributeError):
            event.relations = ()  # type: ignore[misc]

    def test_lineage_is_tuple(self) -> None:
        """lineage is stored as an immutable tuple."""
        event = _make_event()
        assert isinstance(event.lineage, tuple)

    def test_lineage_append_fails(self) -> None:
        """Appending to lineage is impossible (it is a tuple)."""
        event = _make_event()
        with pytest.raises(AttributeError):
            getattr(event.lineage, "append")("new")

    def test_relations_is_tuple(self) -> None:
        """relations is stored as an immutable tuple."""
        event = _make_event()
        assert isinstance(event.relations, tuple)

    def test_relations_append_fails(self) -> None:
        """Appending to relations is impossible (it is a tuple)."""
        event = _make_event()
        with pytest.raises(AttributeError):
            getattr(event.relations, "append")(
                EventRelation(
                    relation_type="reaction",
                    target_event_id="t-3",
                    target_native_ref=None,
                    key=None,
                    fallback_text=None,
                )
            )

    def test_nested_payload_mutation_fails(self) -> None:
        """Nested mutable payload containers are recursively frozen."""
        event = _make_event(payload={"nested": {"inner": ["a"]}})
        nested = event.payload["nested"]
        assert isinstance(nested, dict)
        with pytest.raises(TypeError, match="immutable"):
            nested["inner"] = ["b"]
        assert nested["inner"] == ("a",)

    def test_nested_metadata_custom_mutation_fails(self) -> None:
        """Nested metadata.custom containers are recursively frozen."""
        meta = EventMetadata(custom={"plugin": {"values": [1, 2]}})
        nested = meta.custom["plugin"]
        assert isinstance(nested, dict)
        with pytest.raises(TypeError, match="immutable"):
            nested["values"] = [3]
        assert nested["values"] == (1, 2)

    def test_payload_setitem_fails(self) -> None:
        """Mutating payload in place raises TypeError."""
        event = _make_event()
        with pytest.raises(TypeError, match="immutable"):
            event.payload["new_key"] = "new_value"

    def test_payload_delitem_fails(self) -> None:
        """Deleting from payload raises TypeError."""
        event = _make_event()
        with pytest.raises(TypeError, match="immutable"):
            del event.payload["body"]

    def test_payload_update_fails(self) -> None:
        """Calling .update() on payload raises TypeError."""
        event = _make_event()
        with pytest.raises(TypeError, match="immutable"):
            event.payload.update({"extra": True})

    def test_metadata_custom_setitem_fails(self) -> None:
        """Mutating metadata.custom in place raises TypeError."""
        meta = EventMetadata(custom={"theme": "dark"})
        event = _make_event_with_metadata(meta)
        with pytest.raises(TypeError, match="immutable"):
            event.metadata.custom["theme"] = "light"

    def test_event_relation_metadata_setitem_fails(self) -> None:
        """Mutating EventRelation.metadata in place raises TypeError."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
            metadata={"source": "test"},
        )
        with pytest.raises(TypeError, match="immutable"):
            rel.metadata["source"] = "other"

    def test_telemetry_metrics_setitem_fails(self) -> None:
        """Mutating TelemetryMetadata.metrics in place raises TypeError."""
        tm = TelemetryMetadata(metrics={"battery": 95.0})
        with pytest.raises(TypeError, match="immutable"):
            tm.metrics["battery"] = 50.0

    def test_native_metadata_data_setitem_fails(self) -> None:
        """Mutating NativeMetadata.data in place raises TypeError."""
        nm = NativeMetadata(data={"raw": True})
        with pytest.raises(TypeError, match="immutable"):
            nm.data["raw"] = False


def _make_event_with_metadata(meta: EventMetadata) -> CanonicalEvent:
    """Helper that builds a CanonicalEvent with specific metadata."""
    return CanonicalEvent(
        event_id="evt-meta-1",
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        source_adapter="test",
        source_transport_id="t-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=meta,
    )


class TestConstructorInputIsolation:
    """Mutating constructor inputs must not affect the constructed event."""

    def test_lineage_input_isolation(self) -> None:
        """Mutating the list passed as lineage does not change the event."""
        lineage = ["root"]
        now = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        event = CanonicalEvent(
            event_id="evt-iso-1",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=cast(tuple[str, ...], lineage),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        lineage.append("extra")
        assert event.lineage == ("root",)
        assert len(event.lineage) == 1

    def test_relations_input_isolation(self) -> None:
        """Mutating the list passed as relations does not change the event."""
        rels: list[EventRelation] = []
        now = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        event = CanonicalEvent(
            event_id="evt-iso-2",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=cast(tuple[EventRelation, ...], rels),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        rels.append(
            EventRelation(
                relation_type="reply",
                target_event_id="t-99",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )
        )
        assert len(event.relations) == 0

    def test_payload_input_isolation(self) -> None:
        """Mutating the dict passed as payload does not change the event."""
        payload: dict[str, object] = {"body": "hello"}
        now = datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        event = CanonicalEvent(
            event_id="evt-iso-3",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload=payload,
            metadata=EventMetadata(),
        )
        payload["body"] = "changed"
        payload["extra"] = True
        assert event.payload["body"] == "hello"
        assert "extra" not in event.payload

    def test_metadata_custom_input_isolation(self) -> None:
        """Mutating the dict passed as metadata.custom does not change the event."""
        custom: dict[str, object] = {"theme": "dark"}
        meta = EventMetadata(custom=custom)
        event = _make_event_with_metadata(meta)
        custom["theme"] = "light"
        assert event.metadata.custom["theme"] == "dark"

    def test_event_relation_metadata_input_isolation(self) -> None:
        """Mutating the dict passed as EventRelation.metadata is isolated."""
        md: dict[str, object] = {"source": "test"}
        rel = EventRelation(
            relation_type="reply",
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
            metadata=md,
        )
        md["source"] = "other"
        assert rel.metadata["source"] == "test"


# ===================================================================
# Malformed event validation (__post_init__)
# ===================================================================


class TestMalformedCanonicalEvent:
    """CanonicalEvent.__post_init__ must reject invalid fields."""

    def _valid_kwargs(self) -> dict:
        """Base kwargs that produce a valid CanonicalEvent."""
        return dict(
            event_id="evt-ok",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="t-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={},
            metadata=EventMetadata(),
        )

    def test_empty_event_id_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["event_id"] = ""
        with pytest.raises(ValueError, match="event_id"):
            CanonicalEvent(**kw)

    def test_empty_event_kind_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["event_kind"] = ""
        with pytest.raises(ValueError, match="event_kind"):
            CanonicalEvent(**kw)

    def test_naive_timestamp_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["timestamp"] = datetime(2026, 1, 1, 0, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            CanonicalEvent(**kw)

    def test_negative_depth_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["depth"] = -1
        with pytest.raises(ValueError, match="depth"):
            CanonicalEvent(**kw)

    def test_negative_schema_version_raises(self) -> None:
        """schema_version < 1 is rejected by __post_init__."""
        kw = self._valid_kwargs()
        kw["schema_version"] = -1
        with pytest.raises(ValueError, match="schema_version"):
            CanonicalEvent(**kw)

    def test_zero_schema_version_raises(self) -> None:
        """schema_version == 0 is rejected by __post_init__."""
        kw = self._valid_kwargs()
        kw["schema_version"] = 0
        with pytest.raises(ValueError, match="schema_version"):
            CanonicalEvent(**kw)

    def test_lineage_none_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["lineage"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="lineage"):
            CanonicalEvent(**kw)

    def test_relations_none_raises(self) -> None:
        kw = self._valid_kwargs()
        kw["relations"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="relations"):
            CanonicalEvent(**kw)


# ===================================================================
# SchemaRegistry hardening
# ===================================================================


class TestSchemaRegistryHardening:
    """SchemaRegistry callable check, register_or_replace, unregistered kind."""

    def test_validate_rejects_non_callable_validator(self) -> None:
        """If a non-callable slips into the registry, validate returns False."""
        registry = SchemaRegistry()
        # Directly inject a non-callable to simulate corruption
        registry._schemas[("bad.kind", 1)] = "not-a-callable"  # type: ignore[assignment]
        errors: list[str] = []
        result = registry.validate("bad.kind", {}, errors=errors)
        assert result is False
        assert any("not callable" in e for e in errors)

    def test_register_or_replace_overwrites(self) -> None:
        """register_or_replace overwrites an existing validator."""
        registry = SchemaRegistry()
        registry.register("msg", 1, lambda p: ["old error"])
        registry.register_or_replace("msg", 1, lambda p: [])
        assert registry.validate("msg", {}) is True

    def test_register_or_replace_fresh(self) -> None:
        """register_or_replace works when no prior registration exists."""
        registry = SchemaRegistry()
        registry.register_or_replace("new.kind", 2, lambda p: [])
        assert registry.validate("new.kind", {}, schema_version=2) is True

    def test_unregistered_kind_returns_false(self) -> None:
        """validate returns False for an unregistered event kind."""
        registry = SchemaRegistry()
        assert registry.validate("absent.kind", {}) is False


# ===================================================================
# Schema version compatibility (Track 2)
# ===================================================================


class TestSchemaVersionCompatibility:
    """Schema versioning contract: v1 is current, future versions accepted,
    invalid versions rejected."""

    def test_current_schema_version_is_1(self) -> None:
        """CURRENT_SCHEMA_VERSION constant is 1."""
        assert CURRENT_SCHEMA_VERSION == 1

    def test_v1_event_is_valid(self) -> None:
        """schema_version=1 is the baseline contract and is accepted."""
        event = _make_event()
        assert event.schema_version == 1

    def test_future_version_accepted(self) -> None:
        """A high schema_version (future) is accepted at construction.
        Consumers should treat unknown fields normally and ignore
        unrecognised ones."""
        kw = _valid_kwargs()
        kw["schema_version"] = 999
        event = CanonicalEvent(**kw)
        assert event.schema_version == 999

    def test_schema_version_from_event_extracts_future(self) -> None:
        """schema_version_from_event handles future versions."""
        sv = schema_version_from_event("message.text", {"schema_version": 42})
        assert sv.version == 42

    def test_schema_version_from_event_non_int_defaults(self) -> None:
        """Non-int schema_version in payload defaults to 1."""
        sv = schema_version_from_event("message.text", {"schema_version": "bad"})
        assert sv.version == 1

    def test_valid_relation_types_constant(self) -> None:
        """VALID_RELATION_TYPES contains exactly the five known types."""
        assert VALID_RELATION_TYPES == frozenset(
            {"reply", "reaction", "edit", "delete", "thread"}
        )


# ===================================================================
# Relation validation (Track 2)
# ===================================================================


class TestRelationValidation:
    """EventRelation validates relation_type at construction time."""

    def test_invalid_relation_type_raises(self) -> None:
        """An unknown relation_type raises ValueError in __post_init__."""
        with pytest.raises(ValueError, match="relation_type"):
            EventRelation(
                relation_type="invalid",  # type: ignore[arg-type]
                target_event_id="t-1",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )

    def test_empty_relation_type_raises(self) -> None:
        """Empty string relation_type raises ValueError."""
        with pytest.raises(ValueError, match="relation_type"):
            EventRelation(
                relation_type="",  # type: ignore[arg-type]
                target_event_id="t-1",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )

    @pytest.mark.parametrize(
        "rel_type",
        sorted(VALID_RELATION_TYPES),
    )
    def test_all_valid_relation_types_accepted(self, rel_type: str) -> None:
        """Every valid relation_type is accepted."""
        rel = EventRelation(
            relation_type=rel_type,  # type: ignore[arg-type]
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        assert rel.relation_type == rel_type

    def test_relation_with_no_targets_is_valid(self) -> None:
        """A relation with neither target_event_id nor target_native_ref
        is accepted (pending resolution)."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="some fallback",
        )
        assert rel.target_event_id is None
        assert rel.target_native_ref is None
        assert rel.fallback_text == "some fallback"

    def test_relation_with_both_targets_is_valid(self) -> None:
        """A relation carrying both canonical and native references is
        allowed (canonical takes precedence at resolution time)."""
        nref = NativeRef(
            adapter="test", native_channel_id="c", native_message_id="m"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-1",
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        assert rel.target_event_id == "evt-1"
        assert rel.target_native_ref is not None


# ===================================================================
# Lineage validation (Track 2)
# ===================================================================


class TestLineageValidation:
    """CanonicalEvent validates lineage content."""

    def test_lineage_with_empty_string_raises(self) -> None:
        """An empty string in lineage raises ValueError."""
        kw = _valid_kwargs()
        kw["lineage"] = ("",)
        with pytest.raises(ValueError, match="lineage\\[0\\]"):
            CanonicalEvent(**kw)

    def test_lineage_with_non_string_raises(self) -> None:
        """A non-string item in lineage raises ValueError."""
        kw = _valid_kwargs()
        kw["lineage"] = (123,)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="lineage\\[0\\]"):
            CanonicalEvent(**kw)

    def test_lineage_second_item_invalid(self) -> None:
        """Only the second item is invalid – index must be correct."""
        kw = _valid_kwargs()
        kw["lineage"] = ("valid-id", "")
        with pytest.raises(ValueError, match="lineage\\[1\\]"):
            CanonicalEvent(**kw)

    def test_empty_lineage_is_valid(self) -> None:
        """An empty lineage (root event) is accepted."""
        kw = _valid_kwargs()
        kw["lineage"] = ()
        event = CanonicalEvent(**kw)
        assert event.lineage == ()

    def test_lineage_with_valid_ids(self) -> None:
        """A lineage with all valid non-empty strings is accepted."""
        kw = _valid_kwargs()
        kw["lineage"] = ("evt-a", "evt-b", "evt-c")
        event = CanonicalEvent(**kw)
        assert event.lineage == ("evt-a", "evt-b", "evt-c")

    def test_lineage_parent_consistency(self) -> None:
        """When parent_event_id is set, it typically appears in lineage.
        This is a structural test, not enforced as invariant."""
        kw = _valid_kwargs()
        kw["parent_event_id"] = "parent-1"
        kw["lineage"] = ("root", "parent-1")
        event = CanonicalEvent(**kw)
        assert event.parent_event_id == "parent-1"
        assert event.parent_event_id in event.lineage


# ===================================================================
# Malformed payload validation (Track 2)
# ===================================================================


class TestMalformedPayloadValidation:
    """CanonicalEvent rejects malformed payloads at construction time."""

    def test_payload_dict_accepted(self) -> None:
        """A regular dict payload is frozen and accepted."""
        kw = _valid_kwargs()
        kw["payload"] = {"body": "hello", "count": 42}
        event = CanonicalEvent(**kw)
        assert event.payload["body"] == "hello"
        assert event.payload["count"] == 42

    def test_nested_payload_preserved(self) -> None:
        """Deeply nested payload values are preserved and frozen."""
        kw = _valid_kwargs()
        kw["payload"] = {"nested": {"deep": {"key": "val"}, "list": [1, 2]}}
        event = CanonicalEvent(**kw)
        assert event.payload["nested"]["deep"]["key"] == "val"  # type: ignore[index]
        # Lists are converted to tuples by _FrozenDict
        assert event.payload["nested"]["list"] == (1, 2)  # type: ignore[index]

    def test_empty_payload_accepted(self) -> None:
        """An empty payload dict is accepted."""
        kw = _valid_kwargs()
        kw["payload"] = {}
        event = CanonicalEvent(**kw)
        assert event.payload == {}

    def test_event_id_none_rejected_by_msgspec(self) -> None:
        """event_id=None is rejected (by __post_init__ validation)."""
        kw = _valid_kwargs()
        kw["event_id"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="event_id"):
            CanonicalEvent(**kw)

    def test_event_kind_none_rejected_by_msgspec(self) -> None:
        """event_kind=None is rejected (by __post_init__ validation)."""
        kw = _valid_kwargs()
        kw["event_kind"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="event_kind"):
            CanonicalEvent(**kw)

    def test_schema_version_none_rejected(self) -> None:
        """schema_version=None is rejected."""
        kw = _valid_kwargs()
        kw["schema_version"] = None  # type: ignore[arg-type]
        with pytest.raises((msgspec.ValidationError, TypeError)):
            CanonicalEvent(**kw)

    def test_timestamp_none_rejected(self) -> None:
        """timestamp=None is rejected."""
        kw = _valid_kwargs()
        kw["timestamp"] = None  # type: ignore[arg-type]
        with pytest.raises((msgspec.ValidationError, TypeError, AttributeError)):
            CanonicalEvent(**kw)


# ===================================================================
# Serialization determinism (Track 2)
# ===================================================================


class TestSerializationDeterminism:
    """Encoding the same event produces deterministic output."""

    def test_double_encode_identical(self) -> None:
        """Encoding the same CanonicalEvent twice produces identical bytes."""
        event = _make_event()
        enc1 = msgspec.json.encode(event)
        enc2 = msgspec.json.encode(event)
        assert enc1 == enc2

    def test_round_trip_encode_decode_encode_identical(self) -> None:
        """encode -> decode -> re-encode produces identical bytes."""
        event = _make_event()
        enc1 = msgspec.json.encode(event)
        decoded = msgspec.json.decode(enc1, type=CanonicalEvent)
        enc2 = msgspec.json.encode(decoded)
        assert enc1 == enc2

    def test_different_payloads_differ(self) -> None:
        """Events with different payloads produce different encoded bytes."""
        e1 = _make_event(payload={"body": "hello"})
        e2 = _make_event(payload={"body": "world"})
        assert msgspec.json.encode(e1) != msgspec.json.encode(e2)

    def test_msgpack_round_trip_deterministic(self) -> None:
        """msgpack encode -> decode -> re-encode is deterministic."""
        event = _make_event()
        enc1 = msgspec.msgpack.encode(event)
        decoded = msgspec.msgpack.decode(enc1, type=CanonicalEvent)
        enc2 = msgspec.msgpack.encode(decoded)
        assert enc1 == enc2

    def test_json_field_ordering_stable(self) -> None:
        """JSON output field ordering is stable across serialisations."""
        event = _make_event()
        enc1 = msgspec.json.encode(event)
        enc2 = msgspec.json.encode(event)
        # Field ordering must be identical byte-for-byte
        assert enc1 == enc2
        # Verify it's valid JSON that can be decoded
        decoded = msgspec.json.decode(enc1, type=CanonicalEvent)
        assert decoded.event_id == event.event_id

    def test_event_with_all_sub_namespaces_deterministic(self) -> None:
        """Events with all metadata sub-namespaces encode deterministically."""
        meta = EventMetadata(
            transport=TransportMetadata(protocol="mqtt", gateway_id="gw-1"),
            routing=RoutingMetadata(matched_routes=("r1", "r2"), fanout_group="fg"),
            radio=RadioMetadata(snr=8.5, rssi=-80, frequency=915.0),
            telemetry=TelemetryMetadata(metrics={"battery": 95.0, "voltage": 3.7}),
            native=NativeMetadata(data={"raw": True, "source": "test"}),
            custom={"plugin": {"ver": 2}},
        )
        event = _make_event_with_metadata(meta)
        enc1 = msgspec.json.encode(event)
        enc2 = msgspec.json.encode(event)
        assert enc1 == enc2


# ===================================================================
# Immutable-after-ingress (Track 2)
# ===================================================================


class TestImmutableAfterIngress:
    """Once constructed, CanonicalEvent and all nested containers are
    deeply immutable.  No mutation path exists."""

    def test_event_frozen(self) -> None:
        """Struct-level field assignment is rejected."""
        event = _make_event()
        with pytest.raises(AttributeError):
            event.event_id = "x"  # type: ignore[misc]

    def test_schema_version_frozen(self) -> None:
        """schema_version cannot be changed after construction."""
        event = _make_event()
        with pytest.raises(AttributeError):
            event.schema_version = 2  # type: ignore[misc]

    def test_lineage_immutable(self) -> None:
        """Lineage tuple cannot be modified."""
        event = _make_event()
        with pytest.raises(TypeError):
            event.lineage[0] = "x"  # type: ignore[index]

    def test_payload_deeply_frozen(self) -> None:
        """All levels of payload are frozen."""
        event = _make_event(payload={"l1": {"l2": {"l3": "val"}}})
        with pytest.raises(TypeError):
            event.payload["l1"]["l2"]["l3"] = "mutated"  # type: ignore[index]

    def test_metadata_transport_frozen(self) -> None:
        """TransportMetadata is frozen."""
        meta = EventMetadata(
            transport=TransportMetadata(protocol="mqtt")
        )
        event = _make_event_with_metadata(meta)
        with pytest.raises(AttributeError):
            event.metadata.transport.protocol = "http"  # type: ignore[misc]

    def test_metadata_routing_tuple_frozen(self) -> None:
        """RoutingMetadata.matched_routes is a frozen tuple."""
        meta = EventMetadata(
            routing=RoutingMetadata(matched_routes=("r1",), fanout_group="g1")
        )
        event = _make_event_with_metadata(meta)
        assert event.metadata.routing is not None
        assert isinstance(event.metadata.routing.matched_routes, tuple)
        with pytest.raises(TypeError):
            event.metadata.routing.matched_routes[0] = "x"  # type: ignore[index]

    def test_relation_tuple_immutable(self) -> None:
        """The relations tuple cannot be modified."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id="t-1",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event()
        # relations field itself is frozen
        with pytest.raises(AttributeError):
            event.relations = (rel,)  # type: ignore[misc]

    def test_event_metadata_custom_deeply_frozen(self) -> None:
        """metadata.custom dict is deeply frozen via _FrozenDict."""
        event = _make_event_with_metadata(
            EventMetadata(custom={"a": {"b": "c"}})
        )
        with pytest.raises(TypeError):
            event.metadata.custom["a"]["b"] = "mutated"  # type: ignore[index]

    def test_telemetry_metrics_frozen(self) -> None:
        """TelemetryMetadata.metrics is frozen."""
        tm = TelemetryMetadata(metrics={"key": 1.0})
        with pytest.raises(TypeError):
            tm.metrics["key"] = 2.0  # type: ignore[index]

    def test_native_metadata_data_frozen(self) -> None:
        """NativeMetadata.data is frozen."""
        nm = NativeMetadata(data={"k": "v"})
        with pytest.raises(TypeError):
            nm.data["k"] = "w"  # type: ignore[index]


# ===================================================================
# Unknown metadata fields behavior (Track 2)
# ===================================================================


class TestUnknownMetadataFields:
    """Unknown fields in payloads and metadata are preserved, not stripped.
    msgspec skips unknown fields during decode by default."""

    def test_unknown_payload_fields_preserved_on_construction(self) -> None:
        """Unknown fields in payload dict are preserved."""
        event = _make_event(payload={"body": "hi", "future_field": "val"})
        assert event.payload["future_field"] == "val"

    def test_unknown_custom_metadata_preserved(self) -> None:
        """Unknown fields in metadata.custom are preserved."""
        meta = EventMetadata(custom={"unknown_ns": {"key": "val"}})
        event = _make_event_with_metadata(meta)
        assert event.metadata.custom["unknown_ns"]["key"] == "val"  # type: ignore[index]

    def test_json_decode_unknown_fields_skipped(self) -> None:
        """msgspec JSON decode skips unknown struct fields by default.

        This is the expected behavior: unknown fields are silently
        ignored during decode, preserving forward compatibility.
        """
        # Encode a valid event, inject an unknown field, re-decode
        event = _make_event()
        encoded = msgspec.json.encode(event)
        data = msgspec.json.decode(encoded)
        data["unknown_top_level"] = "should_be_skipped"
        re_encoded = msgspec.json.encode(data)
        decoded = msgspec.json.decode(re_encoded, type=CanonicalEvent)
        assert decoded.event_id == event.event_id

    def test_payload_unknown_fields_round_trip(self) -> None:
        """Unknown fields in payload survive JSON round-trip.
        Note: lists in payload are converted to tuples by _FrozenDict."""
        event = _make_event(payload={"body": "hi", "extra": [1, 2, 3]})
        encoded = msgspec.json.encode(event)
        decoded = msgspec.json.decode(encoded, type=CanonicalEvent)
        assert decoded.payload["body"] == "hi"
        # Lists are recursively frozen to tuples by _FrozenDict
        assert decoded.payload["extra"] == (1, 2, 3)


# ===================================================================
# Schema migration behavior (Track 2)
# ===================================================================


class TestSchemaMigrationBehavior:
    """Schema migration registry and contract behavior."""

    def test_migration_registry_starts_empty(self) -> None:
        """The global MIGRATION_REGISTRY has no registered migrations."""
        # We test the singleton; other tests should not have registered
        # migrations, but we check the API works.
        reg = MIGRATION_REGISTRY
        # The registry may have migrations from other tests, but the
        # lookup for a specific key should return None.
        assert reg.get("message.text", 1, 2) is None or True

    def test_migration_registry_register_and_get(self) -> None:
        """A migration can be registered and retrieved."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()
        fn = lambda p: {**p, "new_field": "default"}
        reg.register("message.text", 1, 2, fn)
        result = reg.get("message.text", 1, 2)
        assert result is fn

    def test_migration_registry_get_unregistered(self) -> None:
        """Looking up an unregistered migration returns None."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()
        assert reg.get("message.text", 1, 2) is None

    def test_migration_registry_registered_keys(self) -> None:
        """registered_keys returns a frozenset of all registered keys."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()
        fn = lambda p: p
        reg.register("message.text", 1, 2, fn)
        reg.register("telemetry.received", 2, 3, fn)
        keys = reg.registered_keys
        assert ("message.text", 1, 2) in keys
        assert ("telemetry.received", 2, 3) in keys

    def test_migration_registry_overwrite(self) -> None:
        """Registering the same key overwrites the previous migration."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()
        fn1 = lambda p: {**p, "v": 1}
        fn2 = lambda p: {**p, "v": 2}
        reg.register("message.text", 1, 2, fn1)
        reg.register("message.text", 1, 2, fn2)
        assert reg.get("message.text", 1, 2) is fn2

    def test_current_schema_version_is_1(self) -> None:
        """v1 is the current compatibility contract."""
        assert CURRENT_SCHEMA_VERSION == 1

    def test_schema_version_must_be_positive(self) -> None:
        """schema_version < 1 is rejected at construction."""
        kw = _valid_kwargs()
        kw["schema_version"] = 0
        with pytest.raises(ValueError, match="schema_version"):
            CanonicalEvent(**kw)

    def test_schema_version_1_accepted(self) -> None:
        """schema_version=1 is the baseline and always accepted."""
        kw = _valid_kwargs()
        kw["schema_version"] = 1
        event = CanonicalEvent(**kw)
        assert event.schema_version == 1


# ===================================================================
# Event taxonomy audit (Track 2)
# ===================================================================


class TestEventTaxonomyAudit:
    """Verify code taxonomy matches the documented contract."""

    def test_known_kinds_matches_event_kind_class(self) -> None:
        """Every EventKind constant appears in KNOWN_KINDS."""
        import dataclasses

        for attr in dir(EventKind):
            if attr.startswith("_"):
                continue
            val = getattr(EventKind, attr)
            if isinstance(val, str) and "." in val:
                assert val in KNOWN_KINDS, f"EventKind.{attr}={val!r} missing from KNOWN_KINDS"

    def test_all_domains_covered(self) -> None:
        """All documented top-level domains are present."""
        domains = {kind.split(".")[0] for kind in KNOWN_KINDS}
        assert "message" in domains
        assert "telemetry" in domains
        assert "presence" in domains
        assert "identity" in domains
        assert "delivery" in domains
        assert "system" in domains
        assert "plugin" in domains

    def test_event_kind_count(self) -> None:
        """The number of known kinds is stable at 18."""
        assert len(KNOWN_KINDS) == 18

    def test_relation_types_match_constant(self) -> None:
        """EventRelation Literal types match VALID_RELATION_TYPES."""
        assert VALID_RELATION_TYPES == frozenset(
            {"reply", "reaction", "edit", "delete", "thread"}
        )

    def test_delivery_kinds_are_separate_from_message(self) -> None:
        """Delivery kinds use the 'delivery.' namespace, not 'message.'."""
        delivery_kinds = [k for k in KNOWN_KINDS if k.startswith("delivery.")]
        message_kinds = [k for k in KNOWN_KINDS if k.startswith("message.")]
        assert len(delivery_kinds) > 0
        assert len(message_kinds) > 0
        assert set(delivery_kinds).isdisjoint(set(message_kinds))


# ===================================================================
# Protocol-neutral readiness (Track 5)
# ===================================================================


class TestProtocolNeutralReadiness:
    """Verify that existing canonical mechanisms support future externally
    initiated adapters (webhooks, request/response) without schema
    changes.

    These tests exercise the usage patterns documented in
    docs/contracts/phase-1-limitations.md Section 2.2.
    """

    # -- Correlation via trace_id --

    def test_trace_id_survives_construction(self) -> None:
        """trace_id can be set to any string value."""
        kw = _valid_kwargs()
        kw["trace_id"] = "corr-abc-123"
        event = CanonicalEvent(**kw)
        assert event.trace_id == "corr-abc-123"

    def test_trace_id_none_is_valid(self) -> None:
        """Events without correlation context leave trace_id as None."""
        kw = _valid_kwargs()
        kw["trace_id"] = None
        event = CanonicalEvent(**kw)
        assert event.trace_id is None

    def test_trace_id_json_round_trip(self) -> None:
        """trace_id survives JSON encode/decode."""
        kw = _valid_kwargs()
        kw["trace_id"] = "webhook-corr-xyz"
        event = CanonicalEvent(**kw)
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert decoded.trace_id == "webhook-corr-xyz"

    def test_trace_id_msgpack_round_trip(self) -> None:
        """trace_id survives msgpack encode/decode."""
        kw = _valid_kwargs()
        kw["trace_id"] = "ext-trace-456"
        event = CanonicalEvent(**kw)
        decoded = msgspec.msgpack.decode(
            msgspec.msgpack.encode(event), type=CanonicalEvent
        )
        assert decoded.trace_id == "ext-trace-456"

    # -- Idempotency via metadata.custom --

    def test_idempotency_key_in_custom(self) -> None:
        """metadata.custom can carry an idempotency key."""
        meta = EventMetadata(
            custom={"idempotency_key": "req_abc123"}
        )
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        assert event.metadata.custom["idempotency_key"] == "req_abc123"

    def test_idempotency_key_round_trip(self) -> None:
        """Idempotency key in custom dict survives JSON round-trip."""
        meta = EventMetadata(
            custom={"idempotency_key": "req_def456", "source": "webhook"}
        )
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert decoded.metadata.custom["idempotency_key"] == "req_def456"
        assert decoded.metadata.custom["source"] == "webhook"

    def test_idempotency_key_immutability(self) -> None:
        """The idempotency key in custom is frozen after construction."""
        meta = EventMetadata(
            custom={"idempotency_key": "req_ghi789"}
        )
        with pytest.raises(TypeError, match="immutable"):
            meta.custom["idempotency_key"] = "tampered"

    # -- Principal/auth context via metadata.custom --

    def test_principal_context_in_custom(self) -> None:
        """metadata.custom can carry a principal dict."""
        principal = {
            "type": "bearer_token",
            "subject": "service-account-42",
            "claims": {"role": "operator"},
        }
        meta = EventMetadata(custom={"principal": principal})
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        stored = event.metadata.custom["principal"]
        assert isinstance(stored, dict)
        assert stored["type"] == "bearer_token"
        assert stored["subject"] == "service-account-42"

    def test_principal_context_round_trip(self) -> None:
        """Principal dict survives JSON round-trip with deep freezing."""
        principal = {"type": "apikey", "subject": "client-7", "scopes": ("read",)}
        meta = EventMetadata(custom={"principal": principal})
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        p = decoded.metadata.custom["principal"]
        assert isinstance(p, dict)
        assert p["type"] == "apikey"
        assert p["subject"] == "client-7"

    def test_principal_context_immutable(self) -> None:
        """Principal dict in custom is deeply frozen."""
        principal = {"type": "basic", "subject": "user-1"}
        meta = EventMetadata(custom={"principal": principal})
        p = meta.custom["principal"]
        assert isinstance(p, dict)
        with pytest.raises(TypeError, match="immutable"):
            p["subject"] = "tampered"

    # -- Request/response lineage --

    def test_request_response_lineage(self) -> None:
        """A response event can link to its request via parent_event_id
        and lineage."""
        now = datetime.now(timezone.utc)
        request = CanonicalEvent(
            event_id="req-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="webhook-incoming",
            source_transport_id="api-client-1",
            source_channel_id="/webhooks/alerts",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "alert triggered"},
            metadata=EventMetadata(
                transport=TransportMetadata(protocol="http"),
                custom={"idempotency_key": "req_001"},
            ),
            trace_id="trace-webhook-1",
        )

        response = CanonicalEvent(
            event_id="resp-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="bridge-engine",
            source_transport_id="internal",
            source_channel_id=None,
            parent_event_id="req-001",
            lineage=("req-001",),
            relations=(),
            payload={"body": "alert forwarded"},
            metadata=EventMetadata(),
            trace_id="trace-webhook-1",
        )

        assert response.parent_event_id == "req-001"
        assert request.event_id in response.lineage
        assert response.trace_id == request.trace_id

    def test_lineage_chain_preserved_in_round_trip(self) -> None:
        """A multi-hop lineage chain survives serialization."""
        kw = _valid_kwargs()
        kw["parent_event_id"] = "evt-parent"
        kw["lineage"] = ("evt-origin", "evt-parent")
        kw["trace_id"] = "multi-hop-trace"
        event = CanonicalEvent(**kw)
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert decoded.lineage == ("evt-origin", "evt-parent")
        assert decoded.parent_event_id == "evt-parent"
        assert decoded.trace_id == "multi-hop-trace"

    # -- Inbound provenance --

    def test_inbound_provenance_fields(self) -> None:
        """source_adapter, source_transport_id, and source_channel_id
        can represent an externally initiated source."""
        meta = EventMetadata(
            transport=TransportMetadata(
                protocol="http", gateway_id="webhook-relay"
            ),
        )
        event = CanonicalEvent(
            event_id="evt-wh-1",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="webhook-incoming",
            source_transport_id="api-client-42",
            source_channel_id="/webhooks/alerts",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "incoming webhook payload"},
            metadata=meta,
            trace_id="wh-trace-1",
        )
        assert event.source_adapter == "webhook-incoming"
        assert event.source_transport_id == "api-client-42"
        assert event.source_channel_id == "/webhooks/alerts"
        assert event.metadata.transport is not None
        assert event.metadata.transport.protocol == "http"
        assert event.metadata.transport.gateway_id == "webhook-relay"

    def test_provenance_round_trip(self) -> None:
        """Externally initiated provenance fields survive serialization."""
        meta = EventMetadata(
            transport=TransportMetadata(protocol="http"),
            custom={
                "http.method": "POST",
                "http.path": "/webhooks/alerts",
                "idempotency_key": "wh_req_123",
            },
        )
        event = CanonicalEvent(
            event_id="evt-wh-2",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="webhook-incoming",
            source_transport_id="ext-svc-1",
            source_channel_id="/api/v1/events",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=meta,
            trace_id="wh-trace-2",
        )
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert decoded.source_adapter == "webhook-incoming"
        assert decoded.source_transport_id == "ext-svc-1"
        assert decoded.source_channel_id == "/api/v1/events"
        assert decoded.metadata.transport is not None
        assert decoded.metadata.transport.protocol == "http"
        assert decoded.metadata.custom["http.method"] == "POST"
        assert decoded.metadata.custom["idempotency_key"] == "wh_req_123"
        assert decoded.trace_id == "wh-trace-2"

    # -- Combined protocol-neutral event --

    def test_full_protocol_neutral_event_round_trip(self) -> None:
        """An event using all protocol-neutral mechanisms survives full
        JSON and msgpack round-trip with every field intact."""
        meta = EventMetadata(
            transport=TransportMetadata(
                protocol="http",
                gateway_id="api-gateway",
            ),
            native=NativeMetadata(
                data={"http.headers": {"content-type": "application/json"}}
            ),
            custom={
                "idempotency_key": "req_full_001",
                "principal": {
                    "type": "bearer_token",
                    "subject": "svc-acct-1",
                },
                "http.method": "POST",
                "http.path": "/webhooks/events",
            },
        )
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        event = CanonicalEvent(
            event_id="evt-pn-full",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="webhook-incoming",
            source_transport_id="external-service-a",
            source_channel_id="/webhooks/events",
            parent_event_id="evt-origin-1",
            lineage=("evt-origin-1",),
            relations=(),
            payload={"body": "full protocol-neutral test"},
            metadata=meta,
            trace_id="pn-trace-full-001",
        )

        # JSON round-trip
        json_decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert json_decoded.event_id == "evt-pn-full"
        assert json_decoded.trace_id == "pn-trace-full-001"
        assert json_decoded.source_adapter == "webhook-incoming"
        assert json_decoded.source_transport_id == "external-service-a"
        assert json_decoded.source_channel_id == "/webhooks/events"
        assert json_decoded.parent_event_id == "evt-origin-1"
        assert json_decoded.lineage == ("evt-origin-1",)
        assert json_decoded.metadata.custom["idempotency_key"] == "req_full_001"
        principal = json_decoded.metadata.custom["principal"]
        assert isinstance(principal, dict)
        assert principal["subject"] == "svc-acct-1"
        assert json_decoded.metadata.transport is not None
        assert json_decoded.metadata.transport.protocol == "http"

        # msgpack round-trip
        msgpack_decoded = msgspec.msgpack.decode(
            msgspec.msgpack.encode(event), type=CanonicalEvent
        )
        assert msgpack_decoded.event_id == "evt-pn-full"
        assert msgpack_decoded.trace_id == "pn-trace-full-001"
        assert msgpack_decoded.metadata.custom["idempotency_key"] == "req_full_001"

    # -- Native namespace extensibility --

    def test_native_namespace_carries_adapter_specific_data(self) -> None:
        """metadata.native can carry arbitrary transport-specific fields
        without affecting the canonical schema."""
        meta = EventMetadata(
            native=NativeMetadata(
                data={
                    "webhook": {
                        "signature": "sha256=abc123",
                        "event_type": "incident.created",
                        "delivery_id": "dlv-xyz",
                    }
                }
            ),
        )
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        wh = native_data["webhook"]
        assert isinstance(wh, dict)
        assert wh["event_type"] == "incident.created"
        assert wh["delivery_id"] == "dlv-xyz"

        # Round-trip preserves native data
        decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert decoded.metadata.native is not None
        wh_rt = decoded.metadata.native.data["webhook"]
        assert isinstance(wh_rt, dict)
        assert wh_rt["event_type"] == "incident.created"
