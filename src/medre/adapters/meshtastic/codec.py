"""Meshtastic adapter codec for converting between native and canonical events.

:class:`MeshtasticCodec` converts raw Meshtastic packet dicts into
:class:`~medre.core.events.canonical.CanonicalEvent` instances.

The codec is deliberately **protobuf-agnostic**: it expects the native
packet to be a plain dict (as produced by the Meshtastic Python library's
callback or a test fake) and does not import ``meshtastic`` directly.
This keeps the codec testable without the mtjk dependency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from medre.adapters.meshtastic.errors import MeshtasticCodecError
from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MeshtasticCodec:
    """Decode helper for the Meshtastic adapter.

    Decode uses :class:`MeshtasticPacketClassifier` as the source of truth
    for portnum normalization, category, ACK detection, channel, packet ID,
    sender identity, and direct-message classification.

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`.
    """

    def __init__(self, adapter_id: str, config: Any) -> None:
        self._adapter_id = adapter_id
        self._config = config
        self._classifier = MeshtasticPacketClassifier(config)

    def decode(
        self,
        packet: dict[str, Any],
        channel_index: int | None = None,
    ) -> CanonicalEvent:
        """Convert a native Meshtastic packet dict into a canonical event.

        Parameters
        ----------
        packet:
            Raw Meshtastic packet dict with native fields.
        channel_index:
            Optional channel index override; defaults to the packet's
            ``channel`` field.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        MeshtasticCodecError
            If the packet is fundamentally unparseable.
        """
        if not isinstance(packet, dict):
            raise MeshtasticCodecError(
                f"packet must be a dict, got {type(packet).__name__}"
            )

        classification = self._classifier.classify(packet)
        category = classification["category"]
        if classification["is_ack"]:
            raise MeshtasticCodecError("ACK packets are not decodable as text events")
        if category != "text":
            raise MeshtasticCodecError(
                f"unsupported Meshtastic packet category for decode: {category!r}"
            )

        decoded = packet.get("decoded", {})
        if isinstance(decoded, dict):
            text = decoded.get("text", "")
        else:
            text = ""

        if text is None:
            text = ""

        sender = classification["sender_id"] or ""
        pkt_channel = (
            channel_index
            if channel_index is not None
            else classification["channel_index"]
        )
        # Fall back to the configured default channel when the packet
        # does not carry an explicit channel index.  Without this,
        # source_channel_id would be None and inbound events would not
        # match routes that filter on source_channel (e.g. "0").
        if pkt_channel is None:
            pkt_channel = self._config.default_channel
        pkt_id = classification["packet_id"]
        portnum = classification["portnum"]

        event_kind = EventKind.MESSAGE_CREATED

        # Build payload
        payload: dict[str, object] = {"body": text}
        if portnum:
            payload["portnum"] = portnum

        # Source native ref from packet ID
        source_native_ref: NativeRef | None = None
        if pkt_id is not None:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=str(pkt_channel) if pkt_channel is not None else None,
                native_message_id=str(pkt_id),
            )

        # Reply relation from replyId
        relations: list[EventRelation] = []
        reply_id = decoded.get("replyId") if isinstance(decoded, dict) else None
        if reply_id:
            relations.append(
                EventRelation(
                    relation_type="reply",
                    target_event_id=None,
                    target_native_ref=NativeRef(
                        adapter=self._adapter_id,
                        native_channel_id=str(pkt_channel) if pkt_channel is not None else None,
                        native_message_id=str(reply_id),
                    ),
                    key=None,
                    fallback_text=None,
                )
            )

        to_id = packet.get("toId", "") or ""

        # longname/shortname are populated by the adapter from the SDK's
        # nodes dict after decode, because text message packets do not
        # carry user info (that comes from separate NODEINFO_APP packets).
        longname = ""
        shortname = ""

        native_meta = NativeMetadata(
            data={
                "packet_id": pkt_id,
                "from_id": sender,
                "channel": pkt_channel,
                "portnum": str(portnum) if portnum else None,
                "to_id": to_id,
                "is_direct_message": classification["is_direct_message"],
                "longname": longname,
                "shortname": shortname,
            }
        )

        metadata = EventMetadata(native=native_meta)

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter=self._adapter_id,
            source_transport_id=sender,
            source_channel_id=str(pkt_channel) if pkt_channel is not None else None,
            parent_event_id=None,
            lineage=(),
            relations=tuple(relations),
            payload=payload,
            metadata=metadata,
            source_native_ref=source_native_ref,
        )
