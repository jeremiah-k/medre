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
from typing import Any, Callable

from medre.adapters.meshtastic.errors import MeshtasticCodecError
from medre.adapters.meshtastic.packet_classifier import (
    MeshtasticPacketClassifier,
)
from medre.adapters.meshtastic.packet_snapshot import snapshot_decoded, snapshot_packet
from medre.core.contracts.adapter import AdapterCodec
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MeshtasticCodec(AdapterCodec):
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
    clock:
        Optional callable returning the current UTC datetime.  Defaults to
        ``lambda: datetime.now(timezone.utc)``.  Inject a deterministic
        clock in tests for reproducible timestamps.
    """

    def __init__(
        self,
        adapter_id: str,
        config: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._config = config
        self._classifier = MeshtasticPacketClassifier(config)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def decode(
        self,
        native_event: Any,
        channel_index: int | None = None,
        node_info: dict[str, str] | None = None,
    ) -> CanonicalEvent:
        """Convert a native Meshtastic packet dict into a canonical event.

        Parameters
        ----------
        native_event:
            Raw Meshtastic packet dict with native fields.
        channel_index:
            Optional channel index override; defaults to the packet's
            ``channel`` field.
        node_info:
            Optional dict with ``longname`` and ``shortname`` keys,
            typically obtained from the session's node database.  When
            provided, the values are embedded into the native metadata
            so that downstream consumers have sender identity without
            a second pass.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        MeshtasticCodecError
            If the packet is fundamentally unparseable.

        Note
        ----
        The adapter is responsible for relay policy gating via
        ClassificationResult.action.  The codec converts text-shaped packets
        and may be used by tests/tools to inspect metadata.
        """
        packet = native_event
        if not isinstance(packet, dict):
            raise MeshtasticCodecError(
                f"packet must be a dict, got {type(packet).__name__}"
            )

        classification = self._classifier.classify(packet)
        if classification.is_ack:
            raise MeshtasticCodecError("ACK packets are not decodable as text events")
        if classification.category != "text":
            raise MeshtasticCodecError(
                f"unsupported Meshtastic packet category for decode: {classification.category!r}"
            )

        decoded = packet.get("decoded", {})
        if isinstance(decoded, dict):
            text = decoded.get("text", "")
        else:
            text = ""

        if text is None:
            text = ""

        sender = classification.from_id or ""
        pkt_channel = (
            channel_index if channel_index is not None else classification.channel_index
        )
        # Fall back to the configured default channel when the packet
        # does not carry an explicit channel index.  Without this,
        # source_channel_id would be None and inbound events would not
        # match routes that filter on source_channel (e.g. "0").
        if pkt_channel is None:
            pkt_channel = self._config.default_channel
        pkt_id = classification.packet_id
        portnum = classification.portnum

        # Determine event kind: reaction vs plain message
        is_reaction = classification.is_reaction
        is_reply = classification.is_reply
        event_kind = EventKind.MESSAGE_CREATED
        if is_reaction:
            event_kind = EventKind.MESSAGE_REACTED

        # Build payload
        payload: dict[str, object] = {"body": text}
        if portnum:
            payload["portnum"] = portnum
        if is_reaction:
            reaction_key = classification.reaction_key or "?"
            payload["key"] = reaction_key

        # Source native ref from packet ID
        source_native_ref: NativeRef | None = None
        if pkt_id is not None:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=str(pkt_channel) if pkt_channel is not None else None,
                native_message_id=str(pkt_id),
            )

        # Relations: reaction or reply from replyId / emoji
        relations: list[EventRelation] = []
        reply_id = classification.reply_id
        emoji_flag = classification.emoji_flag
        if reply_id is not None:
            relation_metadata: dict[str, object] = {
                "meshtastic_reply_id": str(reply_id),
            }
            if emoji_flag:
                relation_metadata["meshtastic_emoji"] = 1
            if is_reaction:
                reaction_key = classification.reaction_key or "?"
                relations.append(
                    EventRelation(
                        relation_type="reaction",
                        target_event_id=None,
                        target_native_ref=NativeRef(
                            adapter=self._adapter_id,
                            native_channel_id=(
                                str(pkt_channel) if pkt_channel is not None else None
                            ),
                            native_message_id=str(reply_id),
                        ),
                        key=reaction_key,
                        fallback_text=None,
                        metadata=relation_metadata,
                    )
                )
            elif is_reply:
                relations.append(
                    EventRelation(
                        relation_type="reply",
                        target_event_id=None,
                        target_native_ref=NativeRef(
                            adapter=self._adapter_id,
                            native_channel_id=(
                                str(pkt_channel) if pkt_channel is not None else None
                            ),
                            native_message_id=str(reply_id),
                        ),
                        key=None,
                        fallback_text=None,
                        metadata=relation_metadata,
                    )
                )

        to_id = packet.get("toId", "") or ""

        # longname/shortname are populated from node_info when provided
        # (obtained via session.get_node_info from the SDK's nodes dict).
        # Text message packets don't carry user info; that comes from
        # separate NODEINFO_APP packets.
        if node_info is not None:
            longname = node_info.get("longname", "")
            shortname = node_info.get("shortname", "")
        else:
            longname = ""
            shortname = ""

        # Emoji raw value from decoded
        emoji_raw = decoded.get("emoji") if isinstance(decoded, dict) else None

        # Transport-specific metadata is namespaced under ``meshtastic.*`` so
        # that native fields stay namespaced by transport.  Namespaced keys
        # are the primary shape; bare keys are retained alongside for legacy
        # stored-event tolerance and current non-identity consumers.
        # Identity labels (longname/shortname) are namespaced-only and are
        # not emitted as bare keys.  ``from_id`` is kept both namespaced and
        # bare because source_native_ref/relation consumers still read it.
        portnum_value = str(portnum) if portnum else None
        native_meta = NativeMetadata(
            data={
                "packet_id": pkt_id,
                "meshtastic.packet_id": pkt_id,
                "from_id": sender,
                "meshtastic.from_id": sender,
                "channel": pkt_channel,
                "meshtastic.channel": pkt_channel,
                "portnum": portnum_value,
                "meshtastic.portnum": portnum_value,
                "to_id": to_id,
                "meshtastic.to_id": to_id,
                "is_direct_message": classification.is_direct_message,
                "meshtastic.is_direct_message": classification.is_direct_message,
                "meshtastic.longname": longname,
                "meshtastic.shortname": shortname,
                "reply_id": reply_id,
                "meshtastic.reply_id": reply_id,
                "emoji": emoji_raw,
                "meshtastic.emoji": emoji_raw,
                "emoji_flag": emoji_flag,
                "meshtastic.emoji_flag": emoji_flag,
                "packet": snapshot_packet(packet),
                "decoded": snapshot_decoded(decoded),
                "classification": {
                    "action": classification.action,
                    "category": classification.category,
                    "reason": classification.reason,
                    "is_reply": is_reply,
                    "is_reaction": is_reaction,
                    "emoji_flag": emoji_flag,
                    "reaction_key": classification.reaction_key,
                    "is_encrypted": classification.is_encrypted,
                    "is_detection_sensor": classification.is_detection_sensor,
                    "routeable": classification.routeable,
                },
            }
        )

        metadata = EventMetadata(native=native_meta)

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=self._clock(),
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
