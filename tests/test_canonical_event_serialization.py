"""Tests for CanonicalEvent serialization, immutability enforcement,
constructor input isolation, and unknown metadata field handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import cast

import msgspec
import pytest

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    MetadataEmbeddingMode,
    NativeMetadata,
    NativeRef,
    RadioMetadata,
    RoutingMetadata,
    TelemetryMetadata,
    TransportMetadata,
)

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
            event.lineage.append("new")

    def test_relations_is_tuple(self) -> None:
        """relations is stored as an immutable tuple."""
        event = _make_event()
        assert isinstance(event.relations, tuple)

    def test_relations_append_fails(self) -> None:
        """Appending to relations is impossible (it is a tuple)."""
        event = _make_event()
        with pytest.raises(AttributeError):
            event.relations.append(
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
        meta = EventMetadata(transport=TransportMetadata(protocol="mqtt"))
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
        event = _make_event_with_metadata(EventMetadata(custom={"a": {"b": "c"}}))
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
