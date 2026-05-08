"""Matrix adapter codec for converting between native and canonical events.

:class:`MatrixCodec` implements the :class:`~medre.adapters.base.AdapterCodec`
interface, converting nio-agnostic event objects into
:class:`~medre.core.events.canonical.CanonicalEvent` instances and back.

The codec is deliberately **nio-agnostic**: it expects the native event
object to carry ``.sender``, ``.body``, ``.event_id``, and ``.source``
attributes but does not import ``nio`` directly.  This keeps the codec
testable without the mindroom-nio dependency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from medre.adapters.base import AdapterCodec
from medre.adapters.matrix.errors import MatrixCodecError
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MatrixCodec(AdapterCodec):
    """Encode / decode helper for the Matrix adapter.

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre.adapters.matrix.config.MatrixConfig` instance.
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

        # Build payload from body + msgtype
        content = source.get("content", {})
        payload: dict[str, object] = {"body": body}
        if "msgtype" in content:
            payload["msgtype"] = content["msgtype"]

        # Native metadata
        native_meta = self._make_native_metadata(room_id, event_id, sender)

        # Extract envelope if present
        envelope = MatrixMetadataEnvelope.from_content(content)

        # Build event metadata
        metadata = EventMetadata(native=native_meta)

        # Resolve relations from envelope if available
        relations: tuple[EventRelation, ...] = ()
        if envelope and envelope.relation_info:
            # The envelope may carry relation context but the actual
            # relation details are extracted separately by the relation
            # handler during inbound processing.
            pass

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=self._adapter_id,
            source_transport_id=sender,
            source_channel_id=room_id,
            parent_event_id=None,
            lineage=(),
            relations=relations,
            payload=payload,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Encode: canonical → native dict
    # ------------------------------------------------------------------

    def encode(self, event: CanonicalEvent, target: Any) -> dict:
        """Encode a canonical event into a Matrix content dict.

        Returns a plain dict suitable for passing to
        ``client.room_send(message_type="m.room.message", content=...)``.

        Parameters
        ----------
        event:
            The canonical event to encode.
        target:
            Adapter-specific target (unused; kept for protocol conformance).

        Returns
        -------
        dict
            A Matrix ``m.room.message`` content dict.
        """
        body = self._extract_body(event)

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
        }

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        return content

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

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
