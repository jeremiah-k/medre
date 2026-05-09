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

from medre.adapters.lxmf.errors import LxmfCodecError


def normalize_lxmf_text(value: object, field_name: str = "content") -> str:
    """Normalize an LXMF text field to str.

    LXMF message content and title are UTF-8 bytes at the wire level.
    This helper normalizes them to str for MEDRE processing.

    Parameters
    ----------
    value:
        The raw value (str, bytes, bytearray, or None).
    field_name:
        Human-readable field name for error messages (default "content").

    Returns
    -------
    str
        The decoded string.

    Raises
    ------
    LxmfCodecError
        If value is of an unsupported type or contains invalid UTF-8.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LxmfCodecError(
                f"invalid UTF-8 in {field_name}: {exc}"
            ) from exc
    if isinstance(value, bytearray):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LxmfCodecError(
                f"invalid UTF-8 in {field_name}: {exc}"
            ) from exc
    raise LxmfCodecError(
        f"unsupported {field_name} type: {type(value).__name__}"
    )


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

        # Detect content presence (str, bytes, or bytearray)
        raw_content = packet.get("content")
        has_content = (
            raw_content is not None
            and isinstance(raw_content, (str, bytes, bytearray))
            and len(raw_content) > 0
        )

        category = "unknown"

        if has_content:
            category = "text"
        elif has_fields:
            # Has fields but no content — e.g. attachment-only
            category = "unsupported"

        # Normalize text fields via helper (may raise on bad UTF-8)
        normalized_content = normalize_lxmf_text(
            packet.get("content", ""), "content"
        )
        normalized_title = normalize_lxmf_text(
            packet.get("title", ""), "title"
        )

        return {
            "category": category,
            "is_direct_message": True,
            "channel_index": None,
            "packet_id": packet_id,
            "sender_id": sender_id,
            "has_fields": has_fields,
            "is_ack": False,
            "content": normalized_content,
            "title": normalized_title,
        }
