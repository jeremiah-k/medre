"""Canonical native-metadata fixtures for built-in transport tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medre.adapters.lxmf.event_shape import LXMF_NATIVE_SCHEMA_VERSION
from medre.adapters.matrix.event_shape import MATRIX_NATIVE_SCHEMA_VERSION
from medre.adapters.meshcore.event_shape import MESHCORE_NATIVE_SCHEMA_VERSION
from medre.adapters.meshtastic.event_shape import MESHTASTIC_NATIVE_SCHEMA_VERSION


def _native_data(
    namespace: str,
    schema_version: int,
    defaults: Mapping[str, object],
    field_overrides: Mapping[str, object] | None,
    keyword_overrides: Mapping[str, object],
) -> dict[str, object]:
    """Build one fixture with deterministic override precedence.

    ``field_overrides`` is the optional positional mapping supplied by callers;
    ``keyword_overrides`` is applied last so explicit keyword fields win.
    """
    native: dict[str, object] = {"schema_version": schema_version, **defaults}
    if field_overrides is not None:
        native.update(field_overrides)
    native.update(keyword_overrides)
    return {namespace: native}


def matrix_native_data(
    field_overrides: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete Matrix v1 native-metadata fixture."""
    return _native_data(
        "matrix",
        MATRIX_NATIVE_SCHEMA_VERSION,
        {
            "room_id": "!test:example.com",
            "event_id": "$test-event",
            "event_type": "m.room.message",
            "sender": "@test:example.com",
            "encryption": {
                "event_encrypted": False,
                "decrypted": False,
            },
        },
        field_overrides,
        overrides,
    )


def meshtastic_native_data(
    field_overrides: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete Meshtastic v1 native-metadata fixture."""
    return _native_data(
        "meshtastic",
        MESHTASTIC_NATIVE_SCHEMA_VERSION,
        {
            "packet_id": None,
            "from_id": "",
            "channel": None,
            "portnum": None,
            "to_id": "",
            "is_direct_message": False,
            "longname": "",
            "shortname": "",
            "reply_id": None,
            "emoji": None,
            "emoji_flag": False,
            "packet": {},
            "decoded": {},
            "classification": {
                "action": "relay",
                "category": "text",
                "reason": "test_fixture",
                "is_reply": False,
                "is_reaction": False,
                "emoji_flag": False,
                "reaction_key": None,
                "is_encrypted": False,
                "is_detection_sensor": False,
                "routeable": True,
            },
        },
        field_overrides,
        overrides,
    )


def meshcore_native_data(
    field_overrides: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete MeshCore v1 native-metadata fixture."""
    return _native_data(
        "meshcore",
        MESHCORE_NATIVE_SCHEMA_VERSION,
        {
            "packet_id": None,
            "sender_id": "",
            "channel": None,
            "pubkey_prefix": "",
            "txt_type": None,
            "is_direct_message": False,
            "contact_label": None,
            "contact_short_label": None,
            "classification": {
                "action": "relay",
                "category": "text",
                "reason": "test_fixture",
                "is_direct_message": False,
                "routeable": True,
            },
        },
        field_overrides,
        overrides,
    )


def lxmf_native_data(
    field_overrides: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete LXMF v1 native-metadata fixture."""
    return _native_data(
        "lxmf",
        LXMF_NATIVE_SCHEMA_VERSION,
        {
            "source_hash": "",
            "destination_hash": None,
            "message_id": None,
            "timestamp": None,
            "title": "",
            "delivery_method": None,
            "has_fields": False,
        },
        field_overrides,
        overrides,
    )
