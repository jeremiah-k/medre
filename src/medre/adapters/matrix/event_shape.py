"""Stable Matrix-native metadata projection for canonical events.

The core event envelope deliberately stays transport-neutral.  Matrix-specific
identity, relation, media, relay, and crypto provenance therefore lives under
a versioned adapter-owned namespace in ``EventMetadata.native.data``.

This module contains only pure projection helpers and imports no Matrix SDK.
That keeps the shape reusable by external producers such as MMRelay without
requiring MEDRE's runtime or mindroom-nio.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope

MATRIX_NATIVE_NAMESPACE = "matrix"
MATRIX_NATIVE_SCHEMA_VERSION = 1
INTEROP_NAMESPACE = "interop"
MMRELAY_INTEROP_NAMESPACE = "mmrelay"

MEDIA_MSGTYPES: dict[str, str] = {
    "m.image": "image",
    "m.audio": "audio",
    "m.video": "video",
    "m.file": "file",
}


def _nonempty_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_int(value: object) -> int | None:
    """Return a schema-safe nonnegative integer, excluding booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


@dataclass(frozen=True, slots=True)
class _MatrixEncryptionMetadata:
    """Typed encryption provenance stored in the Matrix native namespace."""

    event_encrypted: bool
    decrypted: bool
    room_encrypted: bool | None = None
    verified: bool | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize while omitting facts that the producer cannot establish."""
        result: dict[str, object] = {
            "event_encrypted": self.event_encrypted,
            "decrypted": self.decrypted,
        }
        if self.room_encrypted is not None:
            result["room_encrypted"] = self.room_encrypted
        if self.verified is not None:
            result["verified"] = self.verified
        return result


@dataclass(frozen=True, slots=True)
class _MatrixNativeEventMetadata:
    """Typed representation of one versioned Matrix-native metadata object."""

    room_id: str
    event_id: str
    event_type: str
    sender: str
    encryption: _MatrixEncryptionMetadata
    sender_display_name: str | None = None
    origin_server_ts_ms: int | None = None
    transaction_id: str | None = None
    msgtype: str | None = None
    body_format: str | None = None
    formatted_body: str | None = None
    relation: dict[str, object] | None = None
    media: dict[str, object] | None = None
    relay: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the v1 contract using its omission rules."""
        result: dict[str, object] = {
            "schema_version": MATRIX_NATIVE_SCHEMA_VERSION,
            "room_id": self.room_id,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "sender": self.sender,
            "encryption": self.encryption.to_dict(),
        }
        optional_values: tuple[tuple[str, object | None], ...] = (
            ("sender_display_name", self.sender_display_name),
            ("origin_server_ts_ms", self.origin_server_ts_ms),
            ("transaction_id", self.transaction_id),
            ("msgtype", self.msgtype),
            ("format", self.body_format),
            ("formatted_body", self.formatted_body),
            ("relation", self.relation),
            ("media", self.media),
            ("relay", self.relay),
        )
        result.update(
            {key: value for key, value in optional_values if value is not None}
        )
        return result


def matrix_media_descriptor(
    content: dict[str, Any], msgtype: str | None = None
) -> dict[str, object] | None:
    """Return a safe Matrix media descriptor for a message content object.

    Encrypted attachment key material (``key``, ``iv``, hashes, and related
    decryption fields) is intentionally never copied into canonical metadata.
    Only public descriptors needed to identify and describe the attachment are
    retained.  ``msgtype`` is the codec-resolved value and therefore covers
    producers that expose it outside the raw ``content`` mapping.
    """
    resolved = msgtype if isinstance(msgtype, str) else content.get("msgtype")
    if not isinstance(resolved, str) or resolved not in MEDIA_MSGTYPES:
        return None

    encrypted_file = content.get("file")
    encrypted = isinstance(encrypted_file, dict)
    info = content.get("info")
    info_dict = info if isinstance(info, dict) else {}

    mxc_uri = _nonempty_str(content.get("url"))
    if encrypted and mxc_uri is None:
        mxc_uri = _nonempty_str(encrypted_file.get("url"))

    thumbnail_uri = _nonempty_str(info_dict.get("thumbnail_url"))
    thumbnail_file = info_dict.get("thumbnail_file")
    if thumbnail_uri is None and isinstance(thumbnail_file, dict):
        thumbnail_uri = _nonempty_str(thumbnail_file.get("url"))

    descriptor: dict[str, object] = {
        "kind": MEDIA_MSGTYPES[resolved],
        "encrypted": encrypted,
    }
    filename = _nonempty_str(content.get("filename"))
    if filename is None and resolved == "m.file":
        filename = _nonempty_str(content.get("body"))

    values: tuple[tuple[str, object | None], ...] = (
        ("mxc_uri", mxc_uri),
        ("filename", filename),
        ("mime_type", _nonempty_str(info_dict.get("mimetype"))),
        ("size_bytes", _optional_int(info_dict.get("size"))),
        ("width", _optional_int(info_dict.get("w"))),
        ("height", _optional_int(info_dict.get("h"))),
        ("duration_ms", _optional_int(info_dict.get("duration"))),
        ("thumbnail_mxc_uri", thumbnail_uri),
    )
    descriptor.update({key: value for key, value in values if value is not None})
    return descriptor


def matrix_relay_metadata(content: dict[str, Any]) -> dict[str, object] | None:
    """Return the safe MEDRE relay envelope embedded in Matrix content."""
    envelope = MatrixMetadataEnvelope.from_content(content)
    if envelope is None:
        return None
    string_fields = (
        envelope.canonical_event_id,
        envelope.source_adapter,
        envelope.source_channel,
        envelope.provenance,
        envelope.relation_info,
        envelope.lineage_pointer,
        envelope.metadata_mode,
        envelope.native_source_summary,
    )
    if (
        isinstance(envelope.schema_version, bool)
        or not isinstance(envelope.schema_version, int)
        or envelope.schema_version < 1
        or not all(isinstance(value, str) for value in string_fields)
    ):
        return None
    return {
        "medre_envelope": {
            "schema_version": envelope.schema_version,
            "canonical_event_id": envelope.canonical_event_id,
            "source_adapter": envelope.source_adapter,
            "source_channel": envelope.source_channel,
            "provenance": envelope.provenance,
            "relation_info": envelope.relation_info,
            "lineage_pointer": envelope.lineage_pointer,
            "metadata_mode": envelope.metadata_mode,
            "native_source_summary": envelope.native_source_summary,
        }
    }


def build_matrix_native_metadata(
    *,
    room_id: str,
    event_id: str,
    event_type: str,
    sender: str,
    sender_display_name: str | None,
    origin_server_ts_ms: int | None,
    transaction_id: str | None,
    msgtype: str | None,
    content: dict[str, Any],
    relation: dict[str, object] | None,
    room_encrypted: bool | None,
    event_encrypted: bool,
    decrypted: bool,
    verified: bool | None,
    mmrelay_interop: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the versioned Matrix-native metadata namespace.

    The returned object is suitable for ``NativeMetadata.data``. Raw Matrix
    content is never embedded. In particular, Olm/Megolm session identifiers,
    sender keys, encrypted-media keys, IVs, and hashes are deliberately absent.
    """
    matrix = _MatrixNativeEventMetadata(
        room_id=room_id,
        event_id=event_id,
        event_type=event_type,
        sender=sender,
        encryption=_MatrixEncryptionMetadata(
            event_encrypted=event_encrypted,
            decrypted=decrypted,
            room_encrypted=room_encrypted,
            verified=verified,
        ),
        sender_display_name=_nonempty_str(sender_display_name),
        origin_server_ts_ms=_optional_int(origin_server_ts_ms),
        transaction_id=_nonempty_str(transaction_id),
        msgtype=_nonempty_str(msgtype),
        body_format=_nonempty_str(content.get("format")),
        formatted_body=_nonempty_str(content.get("formatted_body")),
        relation=dict(relation) if relation is not None else None,
        media=matrix_media_descriptor(content, msgtype),
        relay=matrix_relay_metadata(content),
    )
    native: dict[str, object] = {MATRIX_NATIVE_NAMESPACE: matrix.to_dict()}
    if mmrelay_interop is not None:
        native[INTEROP_NAMESPACE] = {MMRELAY_INTEROP_NAMESPACE: dict(mmrelay_interop)}
    return native


def matrix_versioned_namespace(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a syntactically versioned Matrix namespace for platform detection.

    This helper establishes only that ``native.matrix`` carries a positive integer
    schema version.  It deliberately does not interpret version-specific fields;
    callers that project Matrix metadata must continue to use
    :func:`matrix_namespace`.
    """
    matrix = native_data.get(MATRIX_NATIVE_NAMESPACE)
    if not isinstance(matrix, Mapping):
        return {}
    schema_version = matrix.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version < 1
    ):
        return {}
    return matrix


def matrix_namespace(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the current Matrix namespace or an empty mapping."""
    matrix = matrix_versioned_namespace(native_data)
    if matrix.get("schema_version") != MATRIX_NATIVE_SCHEMA_VERSION:
        return {}
    return matrix


def mmrelay_interop_fields(native_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the MMRelay interop wire fields from native metadata.

    Returns an empty mapping when the interop namespace is absent or
    malformed, so callers can treat it as a fallback lookup source.
    """
    interop = native_data.get(INTEROP_NAMESPACE)
    if not isinstance(interop, Mapping):
        return {}
    mmrelay = interop.get(MMRELAY_INTEROP_NAMESPACE)
    return mmrelay if isinstance(mmrelay, Mapping) else {}
