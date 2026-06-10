"""LXMF adapter codec for converting between native and canonical events.

:class:`LxmfCodec` converts raw LXMF message payload dicts into
:class:`~medre.core.events.canonical.CanonicalEvent` instances.

The codec expects the native packet to be a plain dict and does not import
any LXMF or Reticulum library directly.  This keeps the codec testable
without a real LXMF dependency.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from medre.adapters.lxmf.errors import LxmfCodecError
from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.core.contracts.adapter import AdapterCodec
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.events.schema import VALID_RELATION_TYPES


class LxmfCodec(AdapterCodec):
    """Decode helper for the LXMF adapter.

    Implements the :class:`~medre.core.contracts.adapter.AdapterCodec`
    ABC.  Decode uses :class:`LxmfPacketClassifier` as the source of
    truth for category, sender identity, packet ID, and fields
    detection.

    Parameters
    ----------
    adapter_id:
        Identifier of the owning adapter (used for ``source_adapter``).
    config:
        The :class:`~medre/config/adapters/lxmf.LxmfConfig`.
    clock:
        Callable returning the current UTC datetime.  Defaults to
        ``lambda: datetime.now(timezone.utc)``.  Override in tests for
        deterministic timestamps.
    """

    def __init__(
        self,
        adapter_id: str,
        config: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter_id = adapter_id
        self._config = config
        self._classifier = LxmfPacketClassifier(config)
        self._logger = logging.getLogger(f"medre.adapters.lxmf.codec.{adapter_id}")
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else (lambda: datetime.now(timezone.utc))
        )

    def _reconstruct_relations(self, envelope: dict) -> list[EventRelation]:
        """Reconstruct EventRelation objects from a MEDRE envelope dict.

        Gracefully skips invalid or corrupt relation entries.
        """
        raw_relations = envelope.get("relations")
        if not raw_relations or not isinstance(raw_relations, list):
            return []

        relations: list[EventRelation] = []
        for raw in raw_relations:
            if not isinstance(raw, dict):
                self._logger.debug("Skipping non-dict relation entry")
                continue

            relation_type = raw.get("relation_type")
            if not isinstance(relation_type, str):
                self._logger.debug(
                    "Skipping relation with non-string type: %r", relation_type
                )
                continue
            if relation_type not in VALID_RELATION_TYPES:
                self._logger.debug(
                    "Skipping relation with invalid type: %r", relation_type
                )
                continue

            try:
                # Deserialize target_native_ref if present
                target_native_ref: NativeRef | None = None
                raw_ref = raw.get("target_native_ref")
                if isinstance(raw_ref, dict):
                    adapter = raw_ref.get("adapter")
                    msg_id = raw_ref.get("native_message_id")
                    if adapter and msg_id:
                        raw_channel_id = raw_ref.get("native_channel_id")
                        target_native_ref = NativeRef(
                            adapter=str(adapter),
                            native_channel_id=(
                                str(raw_channel_id)
                                if raw_channel_id is not None
                                else None
                            ),
                            native_message_id=str(msg_id),
                        )

                target_event_id = raw.get("target_event_id")
                key = raw.get("key")
                fallback_text = raw.get("fallback_text")
                relations.append(
                    EventRelation(
                        relation_type=relation_type,
                        target_event_id=(
                            target_event_id
                            if isinstance(target_event_id, str)
                            else None
                        ),
                        target_native_ref=target_native_ref,
                        key=key if isinstance(key, str) else None,
                        fallback_text=(
                            fallback_text if isinstance(fallback_text, str) else None
                        ),
                    )
                )
            except (TypeError, ValueError, AttributeError) as exc:
                self._logger.debug("Skipping relation construction error: %s", exc)
                continue

        return relations

    def decode(
        self,
        native_event: Any,
    ) -> CanonicalEvent:
        """Convert a native LXMF message payload dict into a canonical event.

        Parameters
        ----------
        native_event:
            Raw LXMF message payload dict with native fields.

        Returns
        -------
        CanonicalEvent
            The framework-standard event.

        Raises
        ------
        LxmfCodecError
            If the packet is fundamentally unparseable or unsupported.
        """
        if not isinstance(native_event, dict):
            raise LxmfCodecError(
                f"packet must be a dict, got {type(native_event).__name__}"
            )

        classification = self._classifier.classify(native_event)
        category = classification["category"]

        if category != "text":
            raise LxmfCodecError(
                f"unsupported LXMF packet category for decode: {category!r}"
            )

        content = classification["content"]
        title = classification["title"]

        sender = classification["sender_id"] or ""
        pkt_id = classification["packet_id"]

        event_kind = EventKind.MESSAGE_CREATED

        # Build payload — include title if present
        payload: dict[str, object] = {
            "body": content,
            "portnum": "lxmf",
        }
        if title:
            payload["title"] = title

        # Source native ref from message_id
        source_native_ref: NativeRef | None = None
        if pkt_id is not None:
            source_native_ref = NativeRef(
                adapter=self._adapter_id,
                native_channel_id=None,
                native_message_id=str(pkt_id),
            )

        # No native reply/reaction relation decoding in LXMF.
        # MEDRE is envelope-only for LXMF relations — it does not read or
        # write LXMF FIELD_THREAD (0x08).  Relation data is carried in the
        # MEDRE fields envelope (0xFD) for cross-transport round-trip only.
        relations: list[EventRelation] = []

        # Build native metadata
        dest_hash = native_event.get("destination_hash")
        if isinstance(dest_hash, bytes):
            dest_hash = dest_hash.hex()

        timestamp = native_event.get("timestamp")

        native_meta_data: dict[str, object] = {
            "source_hash": sender,
            "destination_hash": dest_hash,
            "message_id": pkt_id,
            "timestamp": timestamp,
            "title": title,
            "delivery_method": native_event.get("delivery_method"),
            "has_fields": classification["has_fields"],
        }

        # Check for MEDRE envelope in fields
        fields = native_event.get("fields")
        custom_meta: dict[str, object] = {}
        if fields and isinstance(fields, dict):
            envelope = LxmfFieldsHelper.extract_envelope(fields)
            if envelope is not None:
                custom_meta["medre_envelope"] = envelope
                relations = self._reconstruct_relations(envelope)

        metadata = EventMetadata(
            native=NativeMetadata(data=native_meta_data),
            custom=custom_meta if custom_meta else {},
        )

        return CanonicalEvent(
            event_id=str(uuid.uuid4()),
            event_kind=event_kind,
            schema_version=1,
            timestamp=self._clock(),
            source_adapter=self._adapter_id,
            source_transport_id=sender,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=tuple(relations),
            payload=payload,
            metadata=metadata,
            source_native_ref=source_native_ref,
        )
