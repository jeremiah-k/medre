"""LXMF packet classifier.

:class:`LxmfPacketClassifier` examines raw LXMF message payload dicts
and classifies them by category and direction without routing,
publishing, or storage.

LXMF messages carry fields like ``content``, ``title``, ``source_hash``,
``destination_hash``, ``message_id``, ``fields``, and
``signature_validated``.

The classifier is a pure function: it inspects a packet and returns a
classification dict.  It has no side effects.
"""
from __future__ import annotations

from typing import Any


class LxmfPacketClassifier:
    """Classify raw LXMF message payload dicts.

    LXMF messages are distinguished by payload structure:

    * **Text**: has ``content`` string (may also have ``title`` and
      ``fields``).
    * **Unsupported**: has attachment-like fields but no ``content``.
    * **Unknown**: has neither content nor recognisable structure.

    Parameters
    ----------
    config:
        Optional :class:`~medre.adapters.lxmf.config.LxmfConfig`
        for future use.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def classify(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Classify a raw LXMF message payload dict.

        Parameters
        ----------
        packet:
            Raw message payload dict with LXMF-native fields.

        Returns
        -------
        dict
            Classification result with keys:

            * ``category`` – ``"text"``, ``"unsupported"``, or ``"unknown"``.
            * ``is_direct_message`` – always ``True`` for LXMF DMs.
            * ``channel_index`` – ``None`` (LXMF has no channel concept).
            * ``packet_id`` – message_id hex string, or ``None``.
            * ``sender_id`` – source_hash hex string, or ``None``.
            * ``has_fields`` – whether the fields dict is non-empty.
            * ``is_ack`` – ``False`` (LXMF Acks not classified here).
        """
        content = packet.get("content")
        fields = packet.get("fields")
        source_hash = packet.get("source_hash")
        message_id = packet.get("message_id")

        # Normalise bytes to hex strings
        sender_id: str | None = None
        if source_hash is not None:
            if isinstance(source_hash, bytes):
                sender_id = source_hash.hex()
            else:
                sender_id = str(source_hash)

        packet_id: str | None = None
        if message_id is not None:
            if isinstance(message_id, bytes):
                packet_id = message_id.hex()
            else:
                packet_id = str(message_id)

        has_fields = (
            fields is not None
            and isinstance(fields, dict)
            and len(fields) > 0
        )

        category = "unknown"

        if content is not None and isinstance(content, str):
            category = "text"
        elif has_fields:
            # Has fields but no content — e.g. attachment-only
            category = "unsupported"

        return {
            "category": category,
            "is_direct_message": True,
            "channel_index": None,
            "packet_id": packet_id,
            "sender_id": sender_id,
            "has_fields": has_fields,
            "is_ack": False,
        }
