"""Stable Matrix inbound event-shape contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from medre.adapters.matrix.attribution import project_matrix_attribution
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.event_shape import (
    MATRIX_NATIVE_SCHEMA_VERSION,
    MEDIA_MSGTYPES,
    matrix_media_descriptor,
    matrix_namespace,
    mmrelay_interop_fields,
)
from medre.adapters.matrix.metadata import MATRIX_METADATA_ENVELOPE_SCHEMA_VERSION
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.interop.mmrelay import KEY_ID, KEY_LONGNAME
from tests.helpers.matrix import make_nio_event, make_nio_room, to_event_dict


def _config() -> MatrixConfig:
    return MatrixConfig(
        adapter_id="matrix-1",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )


def _event(
    *,
    content: dict[str, Any],
    event_type: str = "m.room.message",
    event_id: str = "$event:example.com",
    sender: str = "@alice:example.com",
    timestamp_ms: int = 1_700_000_000_123,
    display_name: str = "Alice",
    room_encrypted: bool | None = None,
    decrypted: bool = False,
    event_encrypted: bool | None = None,
    verified: bool | None = None,
    transaction_id: str | None = None,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "type": event_type,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp_ms,
        "content": content,
    }
    return {
        "room_id": "!room:example.com",
        "sender": sender,
        "sender_display_name": display_name,
        "body": content.get("body", ""),
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "msgtype": content.get("msgtype"),
        "server_timestamp": timestamp_ms,
        "transaction_id": transaction_id,
        "room_encrypted": room_encrypted,
        "event_encrypted": decrypted if event_encrypted is None else event_encrypted,
        "decrypted": decrypted,
        "verified": verified,
    }


_SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "schemas"
    / "matrix-native-metadata.schema.json"
)


def _native_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _matrix_data(event: CanonicalEvent) -> dict[str, Any]:
    assert event.metadata.native is not None
    data = event.metadata.native.data["matrix"]
    assert isinstance(data, dict)
    return data


def test_matrix_test_helper_preserves_source_origin_server_timestamp() -> None:
    event = make_nio_event()
    event.source["origin_server_ts"] = 1_700_000_000_321

    normalized = to_event_dict(make_nio_room(), event)

    assert normalized["server_timestamp"] == 1_700_000_000_321


def test_mapping_and_object_inputs_share_typed_normalization() -> None:
    codec = MatrixCodec("matrix-1", _config())
    mapping = _event(content={"msgtype": "m.text", "body": "hello"})
    object_event = SimpleNamespace(**mapping)

    from_mapping = codec.decode(mapping)
    from_object = codec.decode(object_event)

    assert from_mapping.metadata.native.data == from_object.metadata.native.data
    assert from_mapping.payload == from_object.payload
    assert from_mapping.source_channel_id == from_object.source_channel_id


def test_matrix_identity_and_timestamp_are_stably_namespaced() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={"msgtype": "m.text", "body": "hello"},
        transaction_id="txn-7",
    )

    event = codec.decode(native)

    matrix = _matrix_data(event)
    assert set(event.metadata.native.data) == {"matrix"}
    assert matrix["schema_version"] == MATRIX_NATIVE_SCHEMA_VERSION
    assert matrix["room_id"] == "!room:example.com"
    assert matrix["event_id"] == "$event:example.com"
    assert matrix["event_type"] == "m.room.message"
    assert matrix["sender"] == "@alice:example.com"
    assert matrix["sender_display_name"] == "Alice"
    assert matrix["origin_server_ts_ms"] == 1_700_000_000_123
    assert matrix["transaction_id"] == "txn-7"
    assert event.source_transport_id == "@alice:example.com"
    assert event.source_channel_id == "!room:example.com"
    assert event.timestamp == datetime.fromtimestamp(1_700_000_000.123, tz=UTC)


def test_matrix_reply_uses_generic_relation_and_native_wire_context() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={
            "msgtype": "m.text",
            "body": "> <@bob:example.com> old\n\nnew",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$target"}},
        }
    )

    event = codec.decode(native)

    assert event.event_kind == EventKind.MESSAGE_CREATED
    assert event.payload["body"] == "new"
    assert event.relations[0].relation_type == "reply"
    assert event.relations[0].target_native_ref is not None
    assert event.relations[0].target_native_ref.native_message_id == "$target"
    assert _matrix_data(event)["relation"] == {
        "kind": "reply",
        "target_event_id": "$target",
    }


def test_matrix_edit_uses_new_content_and_generic_edit_relation() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={
            "msgtype": "m.text",
            "body": "* stale fallback",
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
            "m.new_content": {
                "msgtype": "m.text",
                "body": "corrected",
                "format": "org.matrix.custom.html",
                "formatted_body": "<strong>corrected</strong>",
            },
        }
    )

    event = codec.decode(native)

    assert event.event_kind == EventKind.MESSAGE_EDITED
    assert event.payload == {"body": "corrected", "msgtype": "m.text"}
    assert event.relations[0].relation_type == "edit"
    assert event.relations[0].target_native_ref.native_message_id == "$target"
    matrix = _matrix_data(event)
    assert matrix["relation"] == {
        "kind": "edit",
        "target_event_id": "$target",
        "rel_type": "m.replace",
    }
    assert matrix["formatted_body"] == "<strong>corrected</strong>"


def test_matrix_thread_takes_precedence_over_reply_fallback() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={
            "msgtype": "m.text",
            "body": "> <@bob:example.com> latest\n\nthread reply",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread-root",
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": "$latest"},
            },
        }
    )

    event = codec.decode(native)

    assert event.payload["body"] == "thread reply"
    assert len(event.relations) == 1
    assert event.relations[0].relation_type == "thread"
    assert event.relations[0].target_native_ref.native_message_id == "$thread-root"
    assert event.source_native_ref is not None
    assert event.source_native_ref.native_thread_id == "$thread-root"
    assert _matrix_data(event)["relation"] == {
        "kind": "thread",
        "target_event_id": "$thread-root",
        "rel_type": "m.thread",
        "reply_to_event_id": "$latest",
        "is_falling_back": True,
    }


def test_matrix_redaction_maps_to_generic_delete_relation() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        event_type="m.room.redaction",
        content={"reason": "spam"},
    )
    native["source"]["redacts"] = "$target"

    event = codec.decode(native)

    assert event.event_kind == EventKind.MESSAGE_DELETED
    assert event.payload == {"reason": "spam"}
    assert event.relations[0].relation_type == "delete"
    assert event.relations[0].target_native_ref.native_message_id == "$target"
    assert _matrix_data(event)["relation"] == {
        "kind": "redaction",
        "target_event_id": "$target",
    }


def test_matrix_reaction_does_not_synthesize_msgtype_metadata() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        event_type="m.reaction",
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$target",
                "key": "👍",
            }
        },
    )

    event = codec.decode(native)

    assert event.event_kind == EventKind.MESSAGE_REACTED
    assert "msgtype" not in _matrix_data(event)


@pytest.mark.parametrize(
    ("msgtype", "kind"),
    tuple(MEDIA_MSGTYPES.items()),
)
def test_media_msgtype_vocabulary_drives_classification_and_descriptor(
    msgtype: str, kind: str
) -> None:
    codec = MatrixCodec("matrix-1", _config())
    event = codec.decode(_event(content={"msgtype": msgtype, "body": "asset"}))

    assert event.event_kind == EventKind.MESSAGE_FILE
    assert _matrix_data(event)["media"]["kind"] == kind


def test_resolved_media_msgtype_drives_descriptor_when_content_omits_msgtype() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"body": "photo.jpg", "url": "mxc://example/media"})
    native["msgtype"] = "m.image"

    event = codec.decode(native)

    assert event.event_kind == EventKind.MESSAGE_FILE
    assert _matrix_data(event)["media"] == {
        "kind": "image",
        "encrypted": False,
        "mxc_uri": "mxc://example/media",
    }


def test_media_descriptor_omits_negative_measurements() -> None:
    descriptor = matrix_media_descriptor(
        {
            "msgtype": "m.video",
            "body": "clip.mp4",
            "info": {"size": -1, "w": -2, "h": 0, "duration": -3},
        }
    )

    assert descriptor is not None
    assert "size_bytes" not in descriptor
    assert "width" not in descriptor
    assert descriptor["height"] == 0
    assert "duration_ms" not in descriptor


def test_normalized_input_falls_back_to_raw_source_identity_fields() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = {
        "source": {
            "type": "m.room.message",
            "room_id": "!source-room:example.com",
            "event_id": "$source-event:example.com",
            "sender": "@source-user:example.com",
            "origin_server_ts": 1_700_000_000_123,
            "content": {"msgtype": "m.text", "body": "source body"},
        }
    }

    event = codec.decode(native)

    assert event.source_transport_id == "@source-user:example.com"
    assert event.source_channel_id == "!source-room:example.com"
    assert event.payload["body"] == "source body"
    assert event.source_native_ref is not None
    assert event.source_native_ref.native_message_id == "$source-event:example.com"


def test_matrix_media_descriptor_is_transport_owned_and_key_safe() -> None:
    codec = MatrixCodec("matrix-1", _config())
    content = {
        "msgtype": "m.image",
        "body": "photo.jpg",
        "file": {
            "url": "mxc://example/media",
            "key": {"k": "secret-material"},
            "iv": "secret-iv",
            "hashes": {"sha256": "secret-hash"},
        },
        "info": {
            "mimetype": "image/jpeg",
            "size": 1234,
            "w": 640,
            "h": 480,
            "thumbnail_file": {
                "url": "mxc://example/thumb",
                "key": {"k": "thumbnail-secret"},
            },
        },
    }

    event = codec.decode(_event(content=content, decrypted=True, verified=True))

    assert event.event_kind == EventKind.MESSAGE_FILE
    media = _matrix_data(event)["media"]
    assert media == {
        "kind": "image",
        "encrypted": True,
        "mxc_uri": "mxc://example/media",
        "mime_type": "image/jpeg",
        "size_bytes": 1234,
        "width": 640,
        "height": 480,
        "thumbnail_mxc_uri": "mxc://example/thumb",
    }
    rendered = repr(event.metadata.native.data)
    assert "secret-material" not in rendered
    assert "secret-iv" not in rendered
    assert "secret-hash" not in rendered
    assert "thumbnail-secret" not in rendered


def test_matrix_crypto_provenance_is_bounded_to_safe_facts() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={"msgtype": "m.text", "body": "encrypted hello"},
        room_encrypted=True,
        decrypted=True,
        verified=False,
    )
    native["sender_key"] = "sensitive-sender-key"
    native["session_id"] = "sensitive-session-id"

    event = codec.decode(native)

    assert _matrix_data(event)["encryption"] == {
        "event_encrypted": True,
        "decrypted": True,
        "room_encrypted": True,
        "verified": False,
    }
    assert event.metadata.transport is not None
    assert event.metadata.transport.protocol == "matrix"
    assert event.metadata.transport.transport_encrypted is True
    rendered = repr(event.metadata.native.data)
    assert "sensitive-sender-key" not in rendered
    assert "sensitive-session-id" not in rendered


def test_matrix_relay_and_mmrelay_metadata_use_separate_namespaces() -> None:
    codec = MatrixCodec("matrix-1", _config())
    content = {
        "msgtype": "m.text",
        "body": "relayed",
        "medre": {
            "envelope": {
                "schema_version": MATRIX_METADATA_ENVELOPE_SCHEMA_VERSION,
                "canonical_event_id": "evt-origin",
                "source_adapter": "mesh-1",
                "source_channel": "LongFast",
                "provenance": "relay",
            }
        },
        KEY_ID: "packet-7",
        KEY_LONGNAME: "Field Node",
    }

    event = codec.decode(_event(content=content))

    native = event.metadata.native.data
    envelope = native["matrix"]["relay"]["medre_envelope"]
    assert envelope["canonical_event_id"] == "evt-origin"
    assert envelope["source_adapter"] == "mesh-1"
    assert native["interop"]["mmrelay"] == {
        KEY_ID: "packet-7",
        KEY_LONGNAME: "Field Node",
    }
    assert KEY_ID not in native["matrix"]


def test_matrix_media_descriptor_returns_none_for_text() -> None:
    assert matrix_media_descriptor({"msgtype": "m.text", "body": "hello"}) is None


def test_interop_fields_accept_frozen_mappings() -> None:
    """mmrelay_interop_fields reads deep-frozen (MappingProxy) namespaces."""
    from types import MappingProxyType

    native = MappingProxyType(
        {
            "interop": MappingProxyType(
                {"mmrelay": MappingProxyType({"meshtastic_id": "7"})}
            )
        }
    )
    assert mmrelay_interop_fields(native).get("meshtastic_id") == "7"
    assert (
        matrix_namespace(
            {"matrix": MappingProxyType({"schema_version": 1, "sender": "@a:b"})}
        ).get("sender")
        == "@a:b"
    )


@pytest.mark.parametrize(
    "matrix",
    [
        {"sender": "@a:b"},
        {"schema_version": 0, "sender": "@a:b"},
        {"schema_version": 2, "sender": "@a:b"},
        {"schema_version": True, "sender": "@a:b"},
    ],
)
def test_matrix_namespace_requires_current_schema_version(
    matrix: dict[str, object],
) -> None:
    assert matrix_namespace({"matrix": matrix}) == {}


def test_matrix_attribution_requires_versioned_namespace() -> None:
    namespaced = project_matrix_attribution(
        {
            "matrix": {
                "schema_version": 1,
                "sender": "@alice:example.com",
                "sender_display_name": "Alice",
            },
            "sender": "@ignored:example.com",
            "displayname": "Ignored",
        }
    )
    assert namespaced["source_sender_id"] == "@alice:example.com"
    assert namespaced["source_sender_label"] == "Alice"

    flat = project_matrix_attribution(
        {"sender": "@ignored:example.com", "displayname": "Ignored"}
    )
    assert flat["source_sender_id"] is None
    assert flat["source_sender_label"] is None


def test_matrix_origin_timestamp_preserves_unix_epoch() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "epoch"})
    native["server_timestamp"] = 0
    native["source"]["origin_server_ts"] = 1_700_000_000_000

    event = codec.decode(native)

    assert event.timestamp.isoformat() == "1970-01-01T00:00:00+00:00"
    assert _matrix_data(event)["origin_server_ts_ms"] == 0


def test_matrix_origin_timestamp_omits_negative_source_value() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "invalid timestamp"})
    native["server_timestamp"] = -1
    native["source"]["origin_server_ts"] = -2

    event = codec.decode(native)

    assert "origin_server_ts_ms" not in _matrix_data(event)


def test_matrix_native_schema_tracks_runtime_version_and_excludes_crypto_secrets() -> (
    None
):
    schema = _native_schema()

    matrix_schema = schema["$defs"]["MatrixEvent"]
    assert (
        matrix_schema["properties"]["schema_version"]["const"]
        == MATRIX_NATIVE_SCHEMA_VERSION
    )
    media_properties = schema["$defs"]["Media"]["properties"]
    assert {"key", "iv", "hashes", "thumbnail_key"}.isdisjoint(media_properties)


def test_direct_normalized_event_derives_safe_crypto_and_unsigned_transaction() -> None:
    codec = MatrixCodec("matrix-1", _config())
    # event_encrypted is left at its None default so the helper tracks
    # decrypted; the codec's safe-crypto derivation path is exercised here.
    native = _event(content={"msgtype": "m.text", "body": "hello"}, decrypted=True)
    native["verified"] = True
    native["transaction_id"] = ""
    native["source"]["unsigned"] = {"transaction_id": "txn-from-unsigned"}

    event = codec.decode(native)
    matrix = _matrix_data(event)

    assert matrix["transaction_id"] == "txn-from-unsigned"
    assert matrix["encryption"]["event_encrypted"] is True
    assert matrix["encryption"]["decrypted"] is True
    assert matrix["encryption"]["verified"] is True


def test_matrix_native_projection_drops_non_scalar_mmrelay_values() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={
            "msgtype": "m.text",
            "body": "hello",
            KEY_ID: {"unexpected": "object"},
            KEY_LONGNAME: "Field Node",
        }
    )

    event = codec.decode(native)

    assert event.metadata.native.data["interop"]["mmrelay"] == {
        KEY_LONGNAME: "Field Node"
    }


def test_matrix_native_projection_drops_malformed_relay_envelope() -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(
        content={
            "msgtype": "m.text",
            "body": "hello",
            "medre": {
                "envelope": {
                    "schema_version": MATRIX_METADATA_ENVELOPE_SCHEMA_VERSION,
                    "canonical_event_id": ["not", "a", "string"],
                }
            },
        }
    )

    event = codec.decode(native)

    assert "relay" not in _matrix_data(event)


def test_matrix_codec_native_output_conforms_to_standalone_schema() -> None:
    codec = MatrixCodec("matrix-1", _config())
    content = {
        "msgtype": "m.image",
        "body": "photo.jpg",
        "url": "mxc://example.org/media",
        "info": {"mimetype": "image/jpeg", "size": 42},
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread-root",
            "m.in_reply_to": {"event_id": "$latest"},
        },
        KEY_ID: 1234,
    }
    event = codec.decode(_event(content=content, room_encrypted=True))
    schema = _native_schema()
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(event.metadata.native.data)


def test_mmrelay_schema_properties_match_runtime_key_set() -> None:
    """MMRelay schema property names track the runtime ``_MMRELAY_KEYS``.

    Drift between the runtime key set and the schema properties is
    caught here so any future addition to one without the other fails
    loudly.
    """
    from medre.adapters.matrix.codec import _MMRELAY_KEYS

    schema = _native_schema()
    props = set(schema["$defs"]["MMRelay"]["properties"])
    assert props == set(_MMRELAY_KEYS)


def test_matrix_origin_timestamp_accepts_numeric_string() -> None:
    """Numeric-string ``server_timestamp`` decodes to int milliseconds."""
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "str ts"})
    native["server_timestamp"] = "1700000000123"

    event = codec.decode(native)

    matrix = _matrix_data(event)
    assert matrix["origin_server_ts_ms"] == 1_700_000_000_123
    assert event.timestamp == datetime.fromtimestamp(1_700_000_000.123, tz=UTC)


def test_matrix_origin_timestamp_omits_invalid_strings() -> None:
    """Invalid / negative string timestamps are omitted from the matrix namespace."""
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "bad str"})
    native["server_timestamp"] = "NaN"

    event = codec.decode(native)

    assert "origin_server_ts_ms" not in _matrix_data(event)

    # Empty string and negative values also drop the field.
    native["server_timestamp"] = ""
    event = codec.decode(native)
    assert "origin_server_ts_ms" not in _matrix_data(event)

    native["server_timestamp"] = "-5"
    event = codec.decode(native)
    assert "origin_server_ts_ms" not in _matrix_data(event)


@pytest.mark.parametrize(
    "raw_timestamp",
    [
        10**100,
        str(10**100),
        1e300,
        float("inf"),
        float("-inf"),
        float("nan"),
    ],
)
def test_matrix_origin_timestamp_rejects_unrepresentable_values(
    raw_timestamp: object,
) -> None:
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "bad timestamp"})
    native["server_timestamp"] = raw_timestamp

    before = datetime.now(UTC)
    event = codec.decode(native)
    after = datetime.now(UTC)

    assert "origin_server_ts_ms" not in _matrix_data(event)
    assert before <= event.timestamp <= after


def test_event_timestamp_defensively_handles_out_of_range_value() -> None:
    before = datetime.now(UTC)
    timestamp = MatrixCodec._event_timestamp(10**100)
    after = datetime.now(UTC)

    assert before <= timestamp <= after


def test_matrix_origin_timestamp_falls_back_to_unsigned_age_ts() -> None:
    """``unsigned.age_ts`` provides the secondary Matrix timestamp source."""
    codec = MatrixCodec("matrix-1", _config())
    native = _event(content={"msgtype": "m.text", "body": "age_ts"})
    native.pop("server_timestamp", None)
    native["source"].pop("origin_server_ts", None)
    native["source"]["unsigned"] = {"age_ts": 1_700_000_000_456}

    event = codec.decode(native)

    assert _matrix_data(event)["origin_server_ts_ms"] == 1_700_000_000_456
