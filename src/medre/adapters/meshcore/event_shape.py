"""Versioned MeshCore native-metadata contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from medre.adapters._native_metadata import (
    current_namespace as _current_namespace,
    versioned_namespace as _versioned_namespace,
)

MESHCORE_NATIVE_NAMESPACE = "meshcore"
MESHCORE_NATIVE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class _MeshCoreNativeEvent:
    schema_version: int
    packet_id: object
    sender_id: str
    channel: int | None
    pubkey_prefix: str
    txt_type: object
    is_direct_message: bool
    contact_label: str | None
    contact_short_label: str | None
    classification: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "sender_id": self.sender_id,
            "channel": self.channel,
            "pubkey_prefix": self.pubkey_prefix,
            "txt_type": self.txt_type,
            "is_direct_message": self.is_direct_message,
            "contact_label": self.contact_label,
            "contact_short_label": self.contact_short_label,
            "classification": dict(self.classification),
        }


def build_meshcore_native_metadata(
    *,
    packet_id: object,
    sender_id: str,
    channel: int | None,
    pubkey_prefix: str,
    txt_type: object,
    is_direct_message: bool,
    contact_label: str | None,
    contact_short_label: str | None,
    classification: dict[str, object],
) -> dict[str, object]:
    """Build the sole supported MeshCore CanonicalEvent native shape."""
    event = _MeshCoreNativeEvent(
        schema_version=MESHCORE_NATIVE_SCHEMA_VERSION,
        packet_id=packet_id,
        sender_id=sender_id,
        channel=channel,
        pubkey_prefix=pubkey_prefix,
        txt_type=txt_type,
        is_direct_message=is_direct_message,
        contact_label=contact_label,
        contact_short_label=contact_short_label,
        classification=classification,
    )
    return {MESHCORE_NATIVE_NAMESPACE: event.to_dict()}


def meshcore_versioned_namespace(
    native_data: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return any positively versioned MeshCore namespace for detection."""
    return _versioned_namespace(native_data, MESHCORE_NATIVE_NAMESPACE)


def meshcore_namespace(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the current MeshCore namespace or an empty mapping."""
    return _current_namespace(
        native_data,
        MESHCORE_NATIVE_NAMESPACE,
        MESHCORE_NATIVE_SCHEMA_VERSION,
    )
