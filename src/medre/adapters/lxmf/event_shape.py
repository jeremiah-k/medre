"""Versioned LXMF native-metadata contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from medre.adapters._native_metadata import (
    current_namespace as _current_namespace,
    versioned_namespace as _versioned_namespace,
)

LXMF_NATIVE_NAMESPACE = "lxmf"
LXMF_NATIVE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class _LxmfNativeEvent:
    schema_version: int
    source_hash: str
    destination_hash: str | None
    message_id: object
    timestamp: object
    title: str
    delivery_method: object
    has_fields: bool
    display_name: str | None
    short_name: str | None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_hash": self.source_hash,
            "destination_hash": self.destination_hash,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "title": self.title,
            "delivery_method": self.delivery_method,
            "has_fields": self.has_fields,
        }
        if self.display_name is not None:
            data["display_name"] = self.display_name
        if self.short_name is not None:
            data["short_name"] = self.short_name
        return data


def build_lxmf_native_metadata(
    *,
    source_hash: str,
    destination_hash: str | None,
    message_id: object,
    timestamp: object,
    title: str,
    delivery_method: object,
    has_fields: bool,
    display_name: str | None = None,
    short_name: str | None = None,
) -> dict[str, object]:
    """Build the sole supported LXMF CanonicalEvent native shape."""
    event = _LxmfNativeEvent(
        schema_version=LXMF_NATIVE_SCHEMA_VERSION,
        source_hash=source_hash,
        destination_hash=destination_hash,
        message_id=message_id,
        timestamp=timestamp,
        title=title,
        delivery_method=delivery_method,
        has_fields=has_fields,
        display_name=display_name,
        short_name=short_name,
    )
    return {LXMF_NATIVE_NAMESPACE: event.to_dict()}


def lxmf_versioned_namespace(
    native_data: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return any positively versioned LXMF namespace for detection."""
    return _versioned_namespace(native_data, LXMF_NATIVE_NAMESPACE)


def lxmf_namespace(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the current LXMF namespace or an empty mapping."""
    return _current_namespace(
        native_data,
        LXMF_NATIVE_NAMESPACE,
        LXMF_NATIVE_SCHEMA_VERSION,
    )
