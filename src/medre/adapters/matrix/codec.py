"""Matrix adapter codec for converting native events to canonical events.

:class:`MatrixCodec` implements the :class:`~medre.core.contracts.adapter.AdapterCodec`
interface, converting nio-agnostic event objects into
:class:`~medre.core.events.canonical.CanonicalEvent` instances.

Outbound rendering is handled by
:class:`~medre.adapters.matrix.renderer.MatrixRenderer`; this codec
provides decode-only (native → canonical) conversion.

The codec is deliberately **nio-agnostic**: it expects the native event
object to carry ``.sender``, ``.body``, ``.event_id``, and ``.source``
attributes but does not import ``nio`` directly.  This keeps the codec
testable without the mindroom-nio dependency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from medre.core.contracts.adapter import AdapterCodec
from medre.adapters.matrix.errors import MatrixCodecError
from medre.adapters.matrix.relations import extract_reply_target
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MatrixCodec(AdapterCodec):
    """Decode helper for the Matrix adapter (native → canonical).

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre.config.adapters.matrix.MatrixConfig` instance.
    """

    def __init__(self, adapter_id: str, config: Any) -> None:
        self._adapter_id = adapter_id
        self._config = config

    # ------------------------------------------------------------------
    # Decode: native → canonical
    # ------------------------------------------------------------------

    def decode(self, native_event: Any, room_id: str = "") -> CanonicalEvent:
        """Convert a native Matrix event into a canonical event.

        The *native_event* is expected to have ``.sender``, ``.body``,
        ``.event_id``, and ``.source`` attributes (matching nio's
        ``RoomMessage*`` event objects).

        Parameters
        ----------
        native_event:
            The adapter-specific event object.
        room_id:
            The Matrix room ID where the event was received.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        MatrixCodecError
            If the native event is missing required ``.source`` data.
        """
        source = getattr(native_event, "source", None)
        if source is None:
            raise MatrixCodecError(
                "native_event is missing .source attribute"
            )

        sender = getattr(native_event, "sender", "")
        body = getattr(native_event, "body", "")
        event_id = getattr(native_event, "event_id", "")

        if not body:
            body = ""

        # Build payload from body + msgtype. Matrix message events should always
        # carry msgtype, but older/fake events can omit it; keep canonical
        # Matrix-originated messages schema-compatible instead of emitting a
        # noisy validation warning downstream.
        content = source.get("content", {})
        msgtype = content.get("msgtype") or getattr(native_event, "msgtype", None)
        payload: dict[str, object] = {
            "body": body,
            "msgtype": msgtype if isinstance(msgtype, str) and msgtype else "m.text",
        }

        # Native metadata
        native_meta = self._make_native_metadata(room_id, event_id, sender)

        # Build event metadata
        metadata = EventMetadata(native=native_meta)

        # Populate source_native_ref when Matrix event_id is non-empty.
        source_native_ref: NativeRef | None = None
        if event_id:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=room_id,
                native_message_id=event_id,
            )

        # Resolve relations from envelope if present
        relations: list[EventRelation] = []

        # Extract Matrix reply relation without storage lookup.
        reply_event_id = extract_reply_target(source)
        if reply_event_id:
            relations.append(
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        adapter=self._adapter_id,
                        native_channel_id=room_id,
                        native_message_id=reply_event_id,
                    ),
                    key=None,
                    fallback_text=None,
                )
            )

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=self._event_timestamp(native_event, source),
            source_adapter=self._adapter_id,
            source_transport_id=sender,
            source_channel_id=room_id,
            parent_event_id=None,
            lineage=(),
            relations=tuple(relations),
            payload=payload,
            metadata=metadata,
            source_native_ref=source_native_ref,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _event_timestamp(native_event: Any, source: dict[str, Any]) -> datetime:
        """Return the Matrix event occurrence time as a UTC datetime.

        nio exposes Matrix ``origin_server_ts`` as milliseconds since epoch.
        Some tests/fakes expose equivalent values under related names.  Falling
        back to ``now`` preserves behavior for synthetic events that do not
        model native timestamps.
        """
        raw = (
            getattr(native_event, "server_timestamp", None)
            or getattr(native_event, "origin_server_ts", None)
            or source.get("origin_server_ts")
            or source.get("unsigned", {}).get("age_ts")
        )
        if isinstance(raw, (int, float)):
            # Matrix timestamps are milliseconds. Accept seconds for defensive
            # compatibility with lightweight fakes.
            seconds = raw / 1000 if raw > 10_000_000_000 else raw
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        return datetime.now(timezone.utc)

    @staticmethod
    def _extract_body(event: CanonicalEvent) -> str:
        """Extract text body from a canonical event payload.

        Parameters
        ----------
        event:
            The canonical event.

        Returns
        -------
        str
            The extracted body text, falling back to ``""``.
        """
        return str(event.payload.get("body", event.payload.get("text", "")))

    @staticmethod
    def _make_native_metadata(
        room_id: str,
        event_id: str,
        sender: str,
    ) -> NativeMetadata:
        """Create a :class:`NativeMetadata` instance for a Matrix event.

        Parameters
        ----------
        room_id:
            The Matrix room ID.
        event_id:
            The Matrix event ID.
        sender:
            The Matrix sender user ID.

        Returns
        -------
        NativeMetadata
            Opaque adapter-specific metadata.
        """
        return NativeMetadata(
            data={
                "room_id": room_id,
                "event_id": event_id,
                "sender": sender,
            }
        )
