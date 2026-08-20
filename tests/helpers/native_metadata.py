"""Canonical native-metadata fixtures for built-in transport tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medre.adapters.lxmf.event_shape import LXMF_NATIVE_SCHEMA_VERSION
from medre.adapters.matrix.event_shape import MATRIX_NATIVE_SCHEMA_VERSION
from medre.adapters.meshcore.event_shape import MESHCORE_NATIVE_SCHEMA_VERSION
from medre.adapters.meshtastic.event_shape import MESHTASTIC_NATIVE_SCHEMA_VERSION


def matrix_native_data(
    fields: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete Matrix v1 native-metadata fixture.

    Optional ``fields`` mapping is merged over the defaults; ``overrides``
    are merged on top of that.  Both layers are written into the
    ``matrix`` namespace so callers can shape the inner schema directly.
    """
    native: dict[str, object] = {
        "schema_version": MATRIX_NATIVE_SCHEMA_VERSION,
        "room_id": "!test:example.com",
        "event_id": "$test-event",
        "event_type": "m.room.message",
        "sender": "@test:example.com",
        "encryption": {
            "event_encrypted": False,
            "decrypted": False,
        },
    }
    if fields is not None:
        native.update(fields)
    if overrides:
        native.update(overrides)
    return {"matrix": native}


def meshtastic_native_data(
    fields: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete Meshtastic v1 native-metadata fixture."""
    native: dict[str, object] = {
        "schema_version": MESHTASTIC_NATIVE_SCHEMA_VERSION,
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
    }
    if fields is not None:
        native.update(fields)
    if overrides:
        native.update(overrides)
    return {"meshtastic": native}


def meshcore_native_data(
    fields: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete MeshCore v1 native-metadata fixture."""
    native: dict[str, object] = {
        "schema_version": MESHCORE_NATIVE_SCHEMA_VERSION,
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
    }
    if fields is not None:
        native.update(fields)
    if overrides:
        native.update(overrides)
    return {"meshcore": native}


def lxmf_native_data(
    fields: Mapping[str, object] | None = None,
    /,
    **overrides: Any,
) -> dict[str, object]:
    """Return a schema-complete LXMF v1 native-metadata fixture."""
    native: dict[str, object] = {
        "schema_version": LXMF_NATIVE_SCHEMA_VERSION,
        "source_hash": "",
        "destination_hash": None,
        "message_id": None,
        "timestamp": None,
        "title": "",
        "delivery_method": None,
        "has_fields": False,
    }
    if fields is not None:
        native.update(fields)
    if overrides:
        native.update(overrides)
    return {"lxmf": native}
