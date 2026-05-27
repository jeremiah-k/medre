"""Tests for core event models: CanonicalEvent, EventRelation, NativeRef,
NativeMessageRef, DeliveryReceipt, EventRecordKind, EventKind, SchemaRegistry,
and EventMetadata with sub-namespaces.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.core.events import (
    KNOWN_KINDS,
    CanonicalEvent,
    DeliveryReceipt,
    EventKind,
    EventMetadata,
    EventRecordKind,
    EventRelation,
    NativeMessageRef,
    NativeMetadata,
    NativeRef,
    RadioMetadata,
    RoutingMetadata,
    SchemaRegistry,
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

    def test_default_status_is_queued(self) -> None:
        """Status defaults to 'queued'."""
        receipt = DeliveryReceipt()
        assert receipt.status == "queued"

    def test_all_fields(self) -> None:
        """Every field can be set explicitly."""
        now = datetime.now(timezone.utc)
        receipt = DeliveryReceipt(
            sequence=3,
            receipt_id="rcpt-1",
            event_id="evt-1",
            delivery_plan_id="plan-1",
            target_adapter="target-1",
            status="sent",
            error=None,
            adapter_message_id="native-1",
            next_retry_at=None,
            created_at=now,
        )
        assert receipt.sequence == 3
        assert receipt.status == "sent"
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
            "message.created",
            "message.text",
            "message.reacted",
            "message.edited",
            "message.deleted",
            "message.file",
            "telemetry.received",
            "telemetry.position",
            "presence.changed",
            "identity.updated",
            "delivery.queued",
            "delivery.sent",
            "delivery.failed",
            "system.audit",
            "system.lifecycle",
            "plugin.custom",
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
        registry.register("message.text", 1, lambda p: ["missing 'body'"])
        assert registry.validate("message.text", {}) is False

    def test_validate_fail_populates_errors_list(self) -> None:
        """The errors list is populated with validator output."""
        registry = SchemaRegistry()
        registry.register("message.text", 1, lambda p: ["missing 'body'", "too short"])
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
            transport=TransportMetadata(protocol="mqtt", delivery_confirmed=True),
            routing=RoutingMetadata(matched_routes=("r1", "r2"), fanout_group="fg"),
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
