"""MeshCore packet classifier.

:class:`MeshCorePacketClassifier` examines raw MeshCore event payload dicts
and classifies them by category and direction without routing,
publishing, or storage.

MeshCore packets are simpler than Meshtastic — they carry payload keys
like ``text``, ``pubkey_prefix``, ``channel_idx``, ``type``, ``code``, and
``txt_type`` rather than a portnum enum.

The classifier is a pure function: it inspects a packet and returns a
frozen :class:`ClassificationResult`.  It has no side effects.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, fields
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ClassificationAction = Literal["relay", "ignore", "drop", "deferred"]
"""What the inbound pipeline should do with a classified packet.

* ``"relay"``   – forward through codec → canonical event pipeline.
* ``"ignore"``  – silently skip (ACKs, unknown packets).
* ``"drop"``    – explicitly discard (reserved for future policy).
* ``"deferred"`` – hold for later evaluation (reserved for future policy).
"""

ClassificationCategory = Literal["text", "direct_message", "ack", "malformed", "unknown"]
"""Packet content category."""

# ---------------------------------------------------------------------------
# Reason constants
# ---------------------------------------------------------------------------

REASON_CHANNEL_TEXT = "channel_text_packet"
REASON_DIRECT_TEXT = "direct_text_packet"
REASON_ACK = "ack_packet"
REASON_UNKNOWN = "unknown_packet"
REASON_EMPTY_TEXT = "empty_text_packet"


# ---------------------------------------------------------------------------
# Frozen result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassificationResult:
    """Immutable classification produced by :meth:`MeshCorePacketClassifier.classify`.

    Attributes
    ----------
    action:
        What the inbound pipeline should do with this packet.
    category:
        Content category (``"text"``, ``"ack"``, or ``"unknown"``).
    reason:
        Human-readable reason string explaining the classification.
    channel_index:
        Channel index from the packet, or ``None`` for DMs / missing.
    packet_id:
        ``sender_timestamp`` integer, or ``None`` if absent.
    sender_id:
        ``pubkey_prefix`` string, or ``None`` if absent.
    is_direct_message:
        ``True`` when ``type == "PRIV"`` (MeshCore direct / private message).
    is_ack:
        ``True`` when the packet carries a ``code`` field.
    is_text:
        ``True`` when the packet carries a ``text`` field.
    routeable:
        ``True`` when the packet should enter the codec / canonical pipeline.
    """

    action: ClassificationAction
    category: ClassificationCategory
    reason: str
    channel_index: int | None
    packet_id: int | None
    sender_id: str | None
    is_direct_message: bool
    is_ack: bool
    is_text: bool
    routeable: bool


class MeshCorePacketClassifier:
    """Classify raw MeshCore event payload dicts.

    MeshCore event types are distinguished by payload structure:

    * **CONTACT_MSG_RECV** (DM): has ``text``, ``pubkey_prefix``,
      ``type``=``"PRIV"``, ``txt_type``.
    * **CHANNEL_MSG_RECV** (channel): has ``text``, ``channel_idx``,
      ``type``=``"CHAN"``, ``txt_type``.
    * **ACK**: has ``code``.

    Parameters
    ----------
    config:
        Optional :class:`~medre.config.adapters.meshcore.MeshCoreConfig`
        for channel mapping lookups (unused in current tranche).
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def classify(self, packet: dict[str, Any]) -> ClassificationResult:
        """Classify a raw MeshCore event payload dict.

        Parameters
        ----------
        packet:
            Raw event payload dict with MeshCore-native fields.

        Returns
        -------
        ClassificationResult
            Frozen typed result with action, category, reason, and flags.
        """
        text = packet.get("text")
        code = packet.get("code")
        sender_id = packet.get("pubkey_prefix")
        packet_id = packet.get("sender_timestamp")
        channel_index = packet.get("channel_idx")
        msg_type = packet.get("type")

        is_direct = msg_type == "PRIV"
        is_ack = code is not None
        is_text = text is not None

        # Determine action / category / reason
        if is_ack:
            return ClassificationResult(
                action="ignore",
                category="ack",
                reason=REASON_ACK,
                channel_index=None,
                packet_id=packet_id,
                sender_id=sender_id,
                is_direct_message=False,
                is_ack=True,
                is_text=False,
                routeable=False,
            )

        if is_text:
            if is_direct:
                category: ClassificationCategory = "direct_message"
                reason = REASON_DIRECT_TEXT
            elif msg_type == "CHAN":
                category = "text"
                reason = REASON_CHANNEL_TEXT
            else:
                # Text present but no recognised type — treat as generic text.
                category = "text"
                reason = REASON_CHANNEL_TEXT
            return ClassificationResult(
                action="relay",
                category=category,
                reason=reason,
                channel_index=channel_index if not is_direct else None,
                packet_id=packet_id,
                sender_id=sender_id,
                is_direct_message=is_direct,
                is_ack=False,
                is_text=True,
                routeable=True,
            )

        # No text, no code — check for unrecognised type vs malformed.
        if msg_type is not None and msg_type not in ("PRIV", "CHAN"):
            return ClassificationResult(
                action="deferred",
                category="unknown",
                reason=REASON_UNKNOWN,
                channel_index=None,
                packet_id=packet_id,
                sender_id=sender_id,
                is_direct_message=False,
                is_ack=False,
                is_text=False,
                routeable=False,
            )

        # Malformed: empty dict, random fields, or PRIV/CHAN without text.
        reason = REASON_EMPTY_TEXT if not packet else REASON_UNKNOWN
        return ClassificationResult(
            action="deferred",
            category="malformed",
            reason=reason,
            channel_index=None,
            packet_id=packet_id,
            sender_id=sender_id,
            is_direct_message=False,
            is_ack=False,
            is_text=False,
            routeable=False,
        )
