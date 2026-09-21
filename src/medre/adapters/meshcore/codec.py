"""MeshCore adapter codec for converting between native and canonical events.

:class:`MeshCoreCodec` converts raw MeshCore event payload dicts into
:class:`~medre.core.events.canonical.CanonicalEvent` instances.

The codec expects the native packet to be a plain dict and does not import
any MeshCore library directly.  This keeps the codec testable without a
MeshCore dependency.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from medre.adapters.meshcore.errors import MeshCoreCodecError
from medre.adapters.meshcore.event_shape import build_meshcore_native_metadata
from medre.adapters.meshcore.identity import derive_message_identity
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
            ``None`` for a channel text, the codec falls back to the
            firmware wire-embedded sender name (``"<name>: <text>"``);
            when no wire name is present, or for direct messages, no
            label is injected. Opaque pubkey prefixes are never passed
            here and are never derived.
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

        raw_text = native_event.get("text")
        if raw_text is not None and not isinstance(raw_text, str):
            raise MeshCoreCodecError(
                "packet text must be a string when present, got "
                f"{type(raw_text).__name__}"
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

        # Source native ref: MeshCore provides no native message ID
        # (see identity.py for the SDK/firmware evidence).  The durable
        # idempotency key is the MEDRE-derived identity digest over the
        # identity-bearing fields; the raw sender_timestamp stays
        # available in native metadata as ``packet_id``.
        message_identity = derive_message_identity(
            sender_id=sender,
            channel_index=pkt_channel,
            sender_timestamp=pkt_id,
            txt_type=native_event.get("txt_type"),
            text=text,
            is_direct_message=classification.is_direct_message,
        )
        source_native_ref: NativeRef | None = None
        if message_identity is not None:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=str(pkt_channel) if pkt_channel is not None else None,
                native_message_id=message_identity,
            )

        # No reply relation support in MeshCore
        relations: list[Any] = []
        # MeshCore firmware prepends the sending node's advertised name to
        # group texts on the wire ("<name>: <text>") because the channel
        # protocol carries no sender identity (CHANNEL_MSG_RECV has no
        # pubkey; only DMs do).  When no known-contact label was resolved,
        # lift the wire-embedded name into the attribution label so relay
        # prefixes like "{sender}/{origin_label}: " render the sender
        # instead of an empty field.  DMs carry real identity and are
        # excluded; an explicitly resolved contact label always wins.
        if contact_label is None and not classification.is_direct_message:
            contact_label = _wire_sender_name(text)

        native_meta = NativeMetadata(
            data=build_meshcore_native_metadata(
                packet_id=pkt_id,
                sender_id=sender,
                channel=pkt_channel,
                pubkey_prefix=sender,
                txt_type=native_event.get("txt_type"),
                is_direct_message=classification.is_direct_message,
                contact_label=contact_label,
                contact_short_label=contact_short_label,
                classification={
                    "action": classification.action,
                    "category": classification.category,
                    "reason": classification.reason,
                    "is_direct_message": classification.is_direct_message,
                    "routeable": classification.routeable,
                },
            )
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


# MeshCore firmware wire convention for group texts: the sending node's
# advertised name is prepended as "<name>: <text>" (channel messages carry
# no protocol-level sender identity).  The name charset mirrors advertised
# node names (letters, digits, spaces, and common separators); the
# remainder must be non-empty.  A leading alnum character and a max length
# keep ordinary sentences without the separator from matching.
_WIRE_SENDER_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9 ._@\\/-]{0,30}):\s+\S")


def _wire_sender_name(text: str) -> str | None:
    """Extract the firmware-embedded sender name from a group text.

    Returns the advertised node name when *text* follows the wire
    convention ``"<name>: <text>"``, otherwise ``None``.  Heuristic by
    necessity: the MeshCore channel protocol provides no other sender
    identity for group messages.
    """
    match = _WIRE_SENDER_NAME_RE.match(text)
    if match is None:
        return None
    name = match.group(1).strip()
    return name or None
