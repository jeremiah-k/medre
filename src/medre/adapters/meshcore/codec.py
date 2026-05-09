"""MeshCore adapter codec for converting between native and canonical events.

:class:`MeshCoreCodec` converts raw MeshCore event payload dicts into
:class:`~medre.core.events.canonical.CanonicalEvent` instances.

The codec expects the native packet to be a plain dict and does not import
any MeshCore library directly.  This keeps the codec testable without a
MeshCore dependency.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from medre.adapters.meshcore.errors import MeshCoreCodecError
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
from medre.core.events.canonical import CanonicalEvent, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MeshCoreCodec:
    """Decode helper for the MeshCore adapter.

    Decode uses :class:`MeshCorePacketClassifier` as the source of truth
    for category, ACK detection, channel, packet ID, sender identity,
    and direct-message classification.

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre.adapters.meshcore.config.MeshCoreConfig`.
    """

    def __init__(self, adapter_id: str, config: Any) -> None:
        self._adapter_id = adapter_id
        self._config = config
        self._classifier = MeshCorePacketClassifier(config)

    def decode(
        self,
        packet: dict[str, Any],
        channel_index: int | None = None,
    ) -> CanonicalEvent:
        """Convert a native MeshCore event payload dict into a canonical event.

        Parameters
        ----------
        packet:
            Raw MeshCore event payload dict with native fields.
        channel_index:
            Optional channel index override; defaults to the packet's
            ``channel_idx`` field.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        MeshCoreCodecError
            If the packet is fundamentally unparseable.
        """
        if not isinstance(packet, dict):
            raise MeshCoreCodecError(
                f"packet must be a dict, got {type(packet).__name__}"
            )

        classification = self._classifier.classify(packet)
        category = classification["category"]
        if classification["is_ack"]:
            raise MeshCoreCodecError("ACK packets are not decodable as text events")
        if category != "text":
            raise MeshCoreCodecError(
                f"unsupported MeshCore packet category for decode: {category!r}"
            )

        text = packet.get("text", "")
        if text is None:
            text = ""

        sender = classification["sender_id"] or ""
        pkt_channel = (
            channel_index
            if channel_index is not None
            else classification["channel_index"]
        )
        pkt_id = classification["packet_id"]

        event_kind = EventKind.MESSAGE_CREATED

        # Build payload
        payload: dict[str, object] = {"body": text}

        # Source native ref from sender_timestamp
        source_native_ref: NativeRef | None = None
        if pkt_id is not None:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=str(pkt_channel) if pkt_channel is not None else None,
                native_message_id=str(pkt_id),
            )

        # No reply relation support in MeshCore
        relations: list[Any] = []

        native_meta = NativeMetadata(
            data={
                "packet_id": pkt_id,
                "sender_id": sender,
                "channel": pkt_channel,
                "pubkey_prefix": sender,
                "txt_type": packet.get("txt_type"),
                "is_direct_message": classification["is_direct_message"],
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
