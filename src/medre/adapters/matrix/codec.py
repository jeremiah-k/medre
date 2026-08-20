"""Matrix-native to canonical event normalization.

``MatrixCodec`` is intentionally mindroom-nio agnostic.  The session boundary
reduces SDK objects to plain dictionaries, and this module converts that stable
input into MEDRE's transport-neutral event envelope plus a versioned Matrix
native-metadata namespace.

Matrix relations are represented through the core's existing generic relation
vocabulary.  Matrix-only details (wire relation type, media descriptors, relay
attribution, and crypto provenance) remain under ``metadata.native.data``.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from medre.adapters.matrix.errors import MatrixCodecError
from medre.adapters.matrix.event_shape import (
    MEDIA_MSGTYPES,
    build_matrix_native_metadata,
)
from medre.adapters.matrix.relations import (
    MatrixRelationDescriptor,
    extract_matrix_relation,
    strip_reply_fallback_body,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterCodec
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata, TransportMetadata
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REACTION_KEY,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
)

_MEDIA_MSGTYPES = frozenset(MEDIA_MSGTYPES)
_MAX_ORIGIN_SERVER_TS_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
_MMRELAY_KEYS = (
    KEY_ID,
    KEY_REPLY_ID,
    KEY_TEXT,
    KEY_EMOJI,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_LONGNAME,
    KEY_SHORTNAME,
    KEY_REACTION_KEY,
)


@dataclass(frozen=True)
class _NormalizedMatrixEvent:
    """Typed Matrix input used by the codec after boundary normalization."""

    source: dict[str, Any]
    room_id: str
    sender: str
    sender_display_name: str | None
    body: str
    event_id: str
    event_type: str
    msgtype: str | None
    server_timestamp: object
    transaction_id: str | None
    room_encrypted: bool | None
    event_encrypted: bool
    decrypted: bool
    verified: bool | None


def _mmrelay_interop_metadata(content: dict[str, Any]) -> dict[str, object] | None:
    """Return scalar MMRelay wire fields present in Matrix content, if any."""
    values: dict[str, object] = {}
    for key in _MMRELAY_KEYS:
        if key not in content:
            continue
        value = content[key]
        if value is None or isinstance(value, (str, int, float, bool)):
            values[key] = value
    return values or None


class MatrixCodec(AdapterCodec):
    """Decode Matrix events into MEDRE canonical events."""

    def __init__(self, adapter_id: str, config: MatrixConfig) -> None:
        self._adapter_id = adapter_id
        self._config = config

    def decode(self, native_event: Any, room_id: str = "") -> CanonicalEvent:
        """Convert a normalized Matrix event or nio-like test object.

        The canonical envelope carries only transport-neutral identity and
        relation semantics.  The full stable Matrix projection is stored under
        ``metadata.native.data["matrix"]``; MMRelay compatibility fields, when
        present, are isolated under ``metadata.native.data["interop"]["mmrelay"]``.
        """
        normalized = self._normalized_fields(native_event, room_id)
        source = normalized.source
        content = source.get("content")
        if not isinstance(content, dict):
            content = {}

        room_id = normalized.room_id
        sender = normalized.sender
        event_id = normalized.event_id
        event_type = normalized.event_type
        body = normalized.body
        relation = extract_matrix_relation(source, normalized.event_type)

        effective_content = content
        if relation is not None and relation.kind == "edit":
            new_content = content.get("m.new_content")
            if isinstance(new_content, dict):
                effective_content = {**content, **new_content}
                new_body = new_content.get("body")
                if isinstance(new_body, str):
                    body = new_body

        msgtype = normalized.msgtype
        if relation is not None and relation.kind == "edit":
            msgtype = self._optional_str(effective_content.get("msgtype")) or msgtype
        effective_msgtype = msgtype or "m.text"

        # Matrix reply fallbacks are presentation artifacts, not canonical body.
        # A thread may include m.in_reply_to as fallback context; strip it too.
        if relation is not None and relation.kind in {"reply", "thread"}:
            body = strip_reply_fallback_body(body)

        # MMRelay encodes Meshtastic reactions as m.emote plus compatibility
        # fields rather than a Matrix m.annotation relation.
        mmrelay_relation = self._mmrelay_reaction(content, effective_msgtype, body)
        relations: tuple[EventRelation, ...]
        if relation is None and mmrelay_relation is not None:
            event_kind = EventKind.MESSAGE_REACTED
            relations = (mmrelay_relation,)
            payload: dict[str, object] = {
                "body": body,
                "msgtype": effective_msgtype,
            }
            if (
                mmrelay_relation.key
                and "meshtastic_reaction_key" in mmrelay_relation.metadata
            ):
                payload["key"] = mmrelay_relation.key
        else:
            event_kind = self._event_kind(
                event_type=event_type,
                msgtype=effective_msgtype,
                relation=relation,
            )
            relations = self._canonical_relations(relation, room_id)
            payload = self._payload(
                event_kind=event_kind,
                body=body,
                msgtype=effective_msgtype,
                content=content,
                relation=relation,
            )

        origin_server_ts_ms = self._origin_server_ts_ms(
            normalized.server_timestamp, source
        )
        timestamp = self._event_timestamp(origin_server_ts_ms)
        source_native_ref = self._source_native_ref(
            room_id=room_id,
            event_id=event_id,
            relation=relation,
        )

        event_encrypted = normalized.event_encrypted
        decrypted = normalized.decrypted
        room_encrypted = normalized.room_encrypted
        verified = normalized.verified

        native_data = build_matrix_native_metadata(
            room_id=room_id,
            event_id=event_id,
            event_type=event_type,
            sender=sender,
            sender_display_name=normalized.sender_display_name or sender,
            origin_server_ts_ms=origin_server_ts_ms,
            transaction_id=normalized.transaction_id,
            msgtype=msgtype,
            content=effective_content,
            relation=relation.to_native_metadata() if relation is not None else None,
            room_encrypted=room_encrypted,
            event_encrypted=event_encrypted,
            decrypted=decrypted,
            verified=verified,
            mmrelay_interop=_mmrelay_interop_metadata(effective_content),
        )

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=timestamp,
            source_adapter=self._adapter_id,
            source_transport_id=sender,
            source_channel_id=room_id,
            parent_event_id=None,
            lineage=(),
            relations=relations,
            payload=payload,
            metadata=EventMetadata(
                transport=TransportMetadata(
                    protocol="matrix",
                    transport_encrypted=event_encrypted,
                ),
                native=NativeMetadata(data=native_data),
            ),
            source_native_ref=source_native_ref,
        )

    @staticmethod
    def _field_reader(native_event: Any) -> Callable[[str], object]:
        """Return a common field accessor for mappings and nio-like objects."""
        if isinstance(native_event, dict):
            return native_event.get
        return lambda name: getattr(native_event, name, None)

    def _normalized_fields(
        self, native_event: Any, room_id: str
    ) -> _NormalizedMatrixEvent:
        read = self._field_reader(native_event)
        source = read("source")
        if source is None:
            raise MatrixCodecError("native_event is missing .source attribute")
        if not isinstance(source, dict):
            raise MatrixCodecError("native_event .source must be a mapping")

        content = source.get("content")
        content = content if isinstance(content, dict) else {}
        unsigned = source.get("unsigned")
        unsigned = unsigned if isinstance(unsigned, dict) else {}

        raw_decrypted = read("decrypted")
        decrypted = raw_decrypted if isinstance(raw_decrypted, bool) else False
        if isinstance(native_event, dict):
            raw_event_encrypted = read("event_encrypted")
            event_encrypted = (
                raw_event_encrypted
                if isinstance(raw_event_encrypted, bool)
                else decrypted
            )
            raw_room_encrypted = read("room_encrypted")
            room_encrypted = (
                raw_room_encrypted if isinstance(raw_room_encrypted, bool) else None
            )
            event_type = self._optional_str(read("event_type"))
            if event_type is None:
                event_type = self._optional_str(source.get("type"))
        else:
            event_encrypted = decrypted
            room_encrypted = None
            event_type = self._optional_str(source.get("type"))

        raw_verified = read("verified") if decrypted else None
        verified = raw_verified if isinstance(raw_verified, bool) else None
        transaction_id = self._optional_str(read("transaction_id"))
        if transaction_id is None:
            transaction_id = self._optional_str(unsigned.get("transaction_id"))
        msgtype = self._optional_str(read("msgtype"))
        if msgtype is None:
            msgtype = self._optional_str(content.get("msgtype"))
        server_timestamp = read("server_timestamp")
        if server_timestamp is None:
            server_timestamp = read("origin_server_ts")

        return _NormalizedMatrixEvent(
            source=source,
            room_id=(
                room_id
                or self._string(read("room_id"))
                or self._string(source.get("room_id"))
            ),
            sender=self._string(read("sender")) or self._string(source.get("sender")),
            sender_display_name=self._optional_str(read("sender_display_name")),
            body=self._string(read("body")) or self._string(content.get("body")),
            event_id=(
                self._string(read("event_id")) or self._string(source.get("event_id"))
            ),
            event_type=event_type or "m.room.message",
            msgtype=msgtype,
            server_timestamp=server_timestamp,
            transaction_id=transaction_id,
            room_encrypted=room_encrypted,
            event_encrypted=event_encrypted,
            decrypted=decrypted,
            verified=verified,
        )

    def _canonical_relations(
        self,
        relation: MatrixRelationDescriptor | None,
        room_id: str,
    ) -> tuple[EventRelation, ...]:
        if relation is None:
            return ()
        relation_type = "delete" if relation.kind == "redaction" else relation.kind
        return (
            EventRelation(
                relation_type=relation_type,
                target_event_id=None,
                target_native_ref=NativeRef(
                    adapter=self._adapter_id,
                    native_channel_id=room_id,
                    native_message_id=relation.target_event_id,
                ),
                key=relation.key,
                fallback_text=None,
            ),
        )

    def _mmrelay_reaction(
        self,
        content: dict[str, Any],
        msgtype: str,
        body: str,
    ) -> EventRelation | None:
        reply_id = content.get(KEY_REPLY_ID)
        emoji_flag = content.get(KEY_EMOJI)
        if (
            msgtype != "m.emote"
            or reply_id in (None, "")
            or emoji_flag != EMOJI_FLAG_VALUE
        ):
            return None

        raw_key = content.get(KEY_REACTION_KEY)
        key = str(raw_key).strip() if raw_key is not None else ""
        if not key:
            key = body
        metadata: dict[str, object] = {
            "meshtastic_reply_id": str(reply_id),
            "meshtastic_emoji": emoji_flag,
        }
        if raw_key is not None and str(raw_key).strip():
            metadata["meshtastic_reaction_key"] = str(raw_key).strip()
        return EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key=key,
            fallback_text=None,
            metadata=metadata,
        )

    @staticmethod
    def _event_kind(
        *,
        event_type: str,
        msgtype: str,
        relation: MatrixRelationDescriptor | None,
    ) -> str:
        if event_type == "m.room.redaction" or (
            relation is not None and relation.kind == "redaction"
        ):
            return EventKind.MESSAGE_DELETED
        if relation is not None and relation.kind == "reaction":
            return EventKind.MESSAGE_REACTED
        if relation is not None and relation.kind == "edit":
            return EventKind.MESSAGE_EDITED
        if msgtype in _MEDIA_MSGTYPES:
            return EventKind.MESSAGE_FILE
        return EventKind.MESSAGE_CREATED

    @staticmethod
    def _payload(
        *,
        event_kind: str,
        body: str,
        msgtype: str,
        content: dict[str, Any],
        relation: MatrixRelationDescriptor | None,
    ) -> dict[str, object]:
        if event_kind == EventKind.MESSAGE_DELETED:
            reason = content.get("reason")
            return {"reason": reason} if isinstance(reason, str) and reason else {}
        payload: dict[str, object] = {"body": body, "msgtype": msgtype}
        if relation is not None and relation.kind == "reaction" and relation.key:
            payload["key"] = relation.key
        return payload

    def _source_native_ref(
        self,
        *,
        room_id: str,
        event_id: str,
        relation: MatrixRelationDescriptor | None,
    ) -> NativeRef | None:
        if not event_id:
            return None
        return NativeRef(
            adapter=self._adapter_id,
            native_channel_id=room_id,
            native_message_id=event_id,
            native_thread_id=(
                relation.target_event_id
                if relation is not None and relation.kind == "thread"
                else None
            ),
        )

    @staticmethod
    def _origin_server_ts_ms(
        raw_timestamp: object, source: dict[str, Any]
    ) -> int | None:
        """Resolve an ``origin_server_ts`` millisecond value.

        Prefers *raw_timestamp* (the codec-supplied field), then the
        ``origin_server_ts`` key in *source*, then ``unsigned.age_ts``.
        Numeric strings (e.g. ``"1700000000123"``) are also accepted; booleans
        and negative values are rejected. Float values are accepted only when
        they are finite and integral (fractional floats would be silently
        truncated by ``int()``). Values outside the representable UTC datetime
        range are rejected before conversion.
        """
        raw = raw_timestamp
        if raw is None:
            raw = source.get("origin_server_ts")
        if raw is None:
            unsigned = source.get("unsigned")
            if isinstance(unsigned, dict):
                raw = unsigned.get("age_ts")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw if 0 <= raw <= _MAX_ORIGIN_SERVER_TS_MS else None
        if isinstance(raw, float):
            if (
                not math.isfinite(raw)
                or not raw.is_integer()
                or raw < 0
                or raw > _MAX_ORIGIN_SERVER_TS_MS
            ):
                return None
            return int(raw)
        if isinstance(raw, str):
            try:
                parsed = int(raw.strip())
            except ValueError:
                return None
            return (
                parsed
                if 0 <= parsed <= _MAX_ORIGIN_SERVER_TS_MS
                else None
            )
        return None

    @staticmethod
    def _event_timestamp(origin_server_ts_ms: int | None) -> datetime:
        if origin_server_ts_ms is not None:
            try:
                return datetime.fromtimestamp(origin_server_ts_ms / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                pass
        return datetime.now(UTC)

    @staticmethod
    def _string(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _optional_str(value: object) -> str | None:
        return value if isinstance(value, str) and value.strip() else None
