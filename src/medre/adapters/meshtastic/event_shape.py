"""Versioned Meshtastic native-metadata contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from medre.adapters._native_metadata import (
    current_namespace as _current_namespace,
    versioned_namespace as _versioned_namespace,
)

MESHTASTIC_NATIVE_NAMESPACE = "meshtastic"
MESHTASTIC_NATIVE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class _MeshtasticNativeEvent:
    schema_version: int
    packet_id: object
    from_id: str
    channel: int | None
    portnum: str | None
    to_id: str
    is_direct_message: bool
    longname: str
    shortname: str
    reply_id: object
    emoji: object
    emoji_flag: object
    packet: dict[str, object]
    decoded: dict[str, object]
    classification: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "from_id": self.from_id,
            "channel": self.channel,
            "portnum": self.portnum,
            "to_id": self.to_id,
            "is_direct_message": self.is_direct_message,
            "longname": self.longname,
            "shortname": self.shortname,
            "reply_id": self.reply_id,
            "emoji": self.emoji,
            "emoji_flag": self.emoji_flag,
            "packet": dict(self.packet),
            "decoded": dict(self.decoded),
            "classification": dict(self.classification),
        }


def build_meshtastic_native_metadata(
    *,
    packet_id: object,
    from_id: str,
    channel: int | None,
    portnum: str | None,
    to_id: str,
    is_direct_message: bool,
    longname: str,
    shortname: str,
    reply_id: object,
    emoji: object,
    emoji_flag: object,
    packet: dict[str, object],
    decoded: dict[str, object],
    classification: dict[str, object],
) -> dict[str, object]:
    """Build the sole supported Meshtastic CanonicalEvent native shape."""
    event = _MeshtasticNativeEvent(
        schema_version=MESHTASTIC_NATIVE_SCHEMA_VERSION,
        packet_id=packet_id,
        from_id=from_id,
        channel=channel,
        portnum=portnum,
        to_id=to_id,
        is_direct_message=is_direct_message,
        longname=longname,
        shortname=shortname,
        reply_id=reply_id,
        emoji=emoji,
        emoji_flag=emoji_flag,
        packet=packet,
        decoded=decoded,
        classification=classification,
    )
    return {MESHTASTIC_NATIVE_NAMESPACE: event.to_dict()}


def meshtastic_versioned_namespace(
    native_data: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return any positively versioned Meshtastic namespace for detection."""
    return _versioned_namespace(native_data, MESHTASTIC_NATIVE_NAMESPACE)


def meshtastic_namespace(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the current Meshtastic namespace or an empty mapping."""
    return _current_namespace(
        native_data,
        MESHTASTIC_NATIVE_NAMESPACE,
        MESHTASTIC_NATIVE_SCHEMA_VERSION,
    )
