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
from typing import Any, Callable

from medre.adapters.meshcore.errors import MeshCoreCodecError
from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterCodec
from medre.core.events.canonical import CanonicalEvent, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata


class MeshCoreCodec(AdapterCodec):
    """Decode helper for the MeshCore adapter.

    Decode uses :class:`MeshCorePacketClassifier` as the source of truth
    for category, ACK detection, channel, packet ID, sender identity,
    and direct-message classification.

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre.config.adapters.meshcore.MeshCoreConfig`.
    """

    def __init__(
        self,
        adapter_id: str,
        config: MeshCoreConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._classifier = MeshCorePacketClassifier(config)

    def decode(
        self,
        native_event: dict[str, Any],
        channel_index: int | None = None,
        *,
        contact_label: str | None = None,
        contact_short_label: str | None = None,
    ) -> CanonicalEvent:
        """Convert a native MeshCore event payload dict into a canonical event.

        Parameters
        ----------
        native_event:
            Raw MeshCore event payload dict with native fields.
        channel_index:
            Optional channel index override; defaults to the packet's
            ``channel_idx`` field.
        contact_label:
            Known-contact advertised name for the sender, resolved by
            the adapter from the session's local contacts store.  When
            ``None`` (sender not a known contact), no label is injected
            and the projection leaves ``source_sender_label`` as
            ``None``.  Opaque pubkey prefixes are never passed here.
        contact_short_label:
            Optional abbreviated contact label.  When ``None``, the
            projection derives a compact form from *contact_label*.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        MeshCoreCodecError
            If the packet is fundamentally unparseable.
        """
        if not isinstance(native_event, dict):
            raise MeshCoreCodecError(
                f"packet must be a dict, got {type(native_event).__name__}"
            )

        classification = self._classifier.classify(native_event)
        if classification.is_ack:
            raise MeshCoreCodecError("ACK packets are not decodable as text events")
        # Codec decodes only text-shaped packets (text / direct_message categories).
        # Adapter gates relay policy via ClassificationResult.action before reaching codec.
        if classification.category not in ("text", "direct_message"):
            raise MeshCoreCodecError(
                f"unsupported MeshCore packet category for decode: {classification.category!r}"
            )

        text = native_event.get("text", "")
        if text is None:
            text = ""

        sender = classification.sender_id or ""
        pkt_channel = (
            channel_index if channel_index is not None else classification.channel_index
        )
        pkt_id = classification.packet_id

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
                "meshcore.packet_id": pkt_id,
                "meshcore.sender_id": sender,
                "meshcore.channel": pkt_channel,
                "meshcore.pubkey_prefix": sender,
                "meshcore.txt_type": native_event.get("txt_type"),
                "meshcore.is_direct_message": classification.is_direct_message,
                # Known-contact label enrichment (adapter-local).
                # Populated by the adapter when the session's local
                # contacts store recognises the sender pubkey prefix.
                # None when the sender is not a known contact; opaque
                # pubkey prefixes never appear here.
                "meshcore.contact_label": contact_label,
                "meshcore.contact_short_label": contact_short_label,
                # Nested classification primitives (no raw SDK objects).
                "meshcore.classification": {
                    "action": classification.action,
                    "category": classification.category,
                    "reason": classification.reason,
                    "is_direct_message": classification.is_direct_message,
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
