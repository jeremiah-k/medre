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
