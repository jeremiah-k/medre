"""Meshtastic packet classifier.

:class:`MeshtasticPacketClassifier` examines raw Meshtastic packet dicts
and classifies them by category, direction, and portnum without routing,
publishing, or storage.

The classifier is a pure function: it inspects a packet and returns a
:class:`ClassificationResult`.  It has no side effects.

Classification policy (conservative defaults):

1. Encrypted packet → **drop** (``"encrypted packet"``)
2. Malformed / no decoded payload → **drop** (``"malformed or missing decoded payload"``)
3. Detection sensor → **deferred** (``"detection sensor packets are deferred"``)
4. Ack / admin → **ignore** (``"ack/admin/system message"``)
5. Unknown / custom portnum → **deferred** (``"unknown or custom portnum"``)
6. Telemetry / position / nodeinfo → **ignore** (``"non-chat message type"``)
7. Direct message → **ignore** (``"direct message to specific node"``)
8. Plugin-only → **deferred** (``"plugin_only packets are deferred"``)
9. Empty text → **ignore** (``"empty text"``)
10. Text message (valid decoded text) → **relay** (``"text message"``)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from medre.interop.mmrelay import EMOJI_FLAG_VALUE

# --- Reason constants ---
REASON_TEXT: str = "text message"
REASON_MALFORMED: str = "malformed or missing decoded payload"
REASON_ENCRYPTED: str = "encrypted packet"
REASON_DETECTION_SENSOR: str = "detection sensor packets are deferred"
REASON_ACK_ADMIN: str = "ack/admin/system message"
REASON_UNKNOWN_PORTNUM: str = "unknown or custom portnum"
REASON_NON_CHAT: str = "non-chat message type"
REASON_DIRECT_MESSAGE: str = "direct message to specific node"
REASON_PLUGIN_ONLY: str = "plugin_only packets are deferred"
REASON_EMPTY_TEXT: str = "empty text"
REASON_UNCLASSIFIED: str = "unclassified packet"

# --- Literal type aliases ---
ClassificationAction = Literal["relay", "ignore", "drop", "deferred"]
ClassificationCategory = Literal[
    "text", "ack", "telemetry", "nodeinfo", "position", "admin", "unknown", "plugin_only"
]

# **FIxTURE-SCAFFOLD ONLY** — This numeric map is NOT derived from the real
# Meshtastic protobuf PortNum enum.  It is a MEDRE test fixture approximation
# that does not claim enum accuracy.  The real protobuf PortNum values differ
# significantly (see docs/contracts/10-meshtastic-source-audit.md for the
# authoritative table).
#
# Before real connection work, this map should be replaced with values
# imported from the optional meshtastic package.  The compat module provides
# `get_portnum_table()` which returns real values when the dependency is
# installed, or None otherwise.
_NUMERIC_PORTNUM_MAP: dict[int, str] = {
    0: "routing",  # Fixture scaffold only — real: UNKNOWN_APP
    1: "text_message",  # TEXT_MESSAGE_APP
    2: "text_message_ack",  # Fixture scaffold only — real: REMOTE_HARDWARE_APP
    3: "position",  # POSITION_APP
    4: "nodeinfo",  # NODEINFO_APP
    5: "telemetry",  # Fixture scaffold only — real: ROUTING_APP
    6: "store_forward",  # Fixture scaffold only — real: ADMIN_APP
    7: "waypoint",  # Fixture scaffold only — real: TEXT_MESSAGE_COMPRESSED_APP
    9: "audio",  # AUDIO_APP
    10: "remote_hardware",  # Fixture scaffold only — real: DETECTION_SENSOR_APP
    11: "private",  # Fixture scaffold only — real: ALERT_APP
    68: "paxcounter",  # Fixture scaffold only — real: ZPS_APP
    71: "neighbor_info",  # NEIGHBORINFO_APP
    72: "traceroute",  # TRACEROUTE_APP
}

_SYMBOLIC_PORTNUM_MAP: dict[str, str] = {
    "TEXT_MESSAGE_APP": "text_message",
    "TEXT_MESSAGE_ACK_APP": "text_message_ack",
    "TELEMETRY_APP": "telemetry",
    "POSITION_APP": "position",
    "NODEINFO_APP": "nodeinfo",
    "ADMIN_APP": "admin",
    "ROUTING_APP": "routing",
    "DETECTION_SENSOR_APP": "detection_sensor",
}

_NORMALIZED_PORTNUMS: set[str] = {
    "text_message",
    "text_message_ack",
    "telemetry",
    "position",
    "nodeinfo",
    "admin",
    "routing",
    "detection_sensor",
}


def normalize_portnum(value: object) -> str | None:
    """Return MEDRE's narrow normalized Meshtastic portnum string.

    Supports both current MEDRE fixture strings (``"text_message"``) and
    real symbolic Meshtastic names emitted by meshtastic-python / mtjk
    callback dictionaries (``"TEXT_MESSAGE_APP"``).  Unknown values are
    normalized deterministically but remain unsupported by the classifier.

    .. caution::

       The ``_NUMERIC_PORTNUM_MAP`` used for ``int`` values is **fixture
       scaffold only**.  It does **not** match the real Meshtastic protobuf
       ``PortNum`` enum (see ``docs/contracts/10-meshtastic-source-audit.md``).
       Numeric portnum values should not be treated as protocol authority
       until the map is replaced with values from the optional meshtastic
       package (via ``compat.get_portnum_table()``).
    """
    if value is None:
        return None
    if isinstance(value, int):
        return _NUMERIC_PORTNUM_MAP.get(value, str(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        symbolic = _SYMBOLIC_PORTNUM_MAP.get(stripped.upper())
        if symbolic is not None:
            return symbolic
        lowered = stripped.lower()
        if lowered in _NORMALIZED_PORTNUMS or lowered.startswith("plugin_"):
            return lowered
        return lowered
    return str(value).lower()


def _is_routing_ack(decoded: dict[str, Any]) -> bool:
    """Return True for the narrow ROUTING_APP ACK shape (ACK detection)."""
    routing = decoded.get("routing")
    if not isinstance(routing, dict):
        return False
    error_reason = routing.get("errorReason")
    return error_reason == "NONE"


@dataclass(frozen=True)
class ClassificationResult:
    """Immutable classification result returned by :meth:`MeshtasticPacketClassifier.classify`.

    Attributes
    ----------
    action:
        Disposition: ``"relay"``, ``"ignore"``, ``"drop"``, or ``"deferred"``.
    category:
        Packet category: ``"text"``, ``"ack"``, ``"telemetry"``, ``"nodeinfo"``,
        ``"position"``, ``"admin"``, ``"unknown"``, or ``"plugin_only"``.
    reason:
        Human-readable explanation of why this decision was made.
    portnum:
        Decoded portnum string, or ``None``.
    channel_index:
        Radio channel index, or ``None``.
    packet_id:
        Packet ID integer, or ``None``.
    from_id:
        Sender node ID, or ``None``.
    to_id:
        Recipient node ID string (from ``toId``), or ``""``.
    is_text:
        Whether this is a text message packet.
    is_ack:
        Whether this is an acknowledgement.
    is_encrypted:
        Whether the packet is encrypted.
    is_detection_sensor:
        Whether the packet is a detection sensor packet.
    is_direct_message:
        Whether the packet is a DM.
    routeable:
        Whether the classifier decided the packet can proceed to relay
        (equivalent to ``action == "relay"``).
    reply_id:
        ``decoded.replyId`` integer, or ``None``.
    emoji_flag:
        ``True`` when ``decoded.emoji == 1``.
    reaction_key:
        Stripped text when ``emoji_flag`` is set (``"?"`` if empty),
        else ``None``.
    is_reply:
        Text packet with ``replyId`` but no emoji flag.
    is_reaction:
        Text packet with ``replyId`` and emoji flag.
    """

    action: ClassificationAction
    category: ClassificationCategory
    reason: str
    portnum: str | None
    channel_index: int | None
    packet_id: int | None
    from_id: str | None
    to_id: str
    is_text: bool
    is_ack: bool
    is_encrypted: bool
    is_detection_sensor: bool
    is_direct_message: bool
    routeable: bool
    reply_id: int | None
    emoji_flag: bool
    reaction_key: str | None
    is_reply: bool
    is_reaction: bool


class MeshtasticPacketClassifier:
    """Classify raw Meshtastic packet dicts.

    Classification works correctly for both MEDRE-normalised portnum strings
    and real symbolic ``*_APP`` portnum names.  Numeric portnum resolution
    uses the ``_NUMERIC_PORTNUM_MAP`` which is **fixture scaffold only** —
    see ``normalize_portnum()`` for details.

    Parameters
    ----------
    config:
        Optional :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`
        for channel mapping lookups.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    @staticmethod
    def _is_broadcast(to_id: Any) -> bool:
        """Return True if *to_id* represents a broadcast address.

        Broadcast addresses in Meshtastic:
        * ``""`` (empty string)
        * ``"^all"``
        * ``0xffffffff`` (integer 4294967295)
        * ``"4294967295"`` (string form of 0xffffffff)

        Any other value is a direct message target.
        """
        if to_id == "" or to_id is None:
            return True
        if to_id == "^all":
            return True
        if isinstance(to_id, int) and to_id == 0xFFFFFFFF:
            return True
        if isinstance(to_id, str) and to_id == "4294967295":
            return True
        return False

    def classify(self, packet: dict[str, Any]) -> ClassificationResult:
        """Classify a raw Meshtastic packet dict.

        Parameters
        ----------
        packet:
            Raw packet dict with Meshtastic-native fields.  In addition to
            the canonical ``fromId`` / ``toId`` string fields, real packets
            from *meshtastic-python* may also carry ``from`` (int) and
            ``to`` (int).  The classifier uses ``from`` as a fallback when
            ``fromId`` is absent, and checks ``to`` for the broadcast value
            ``0xFFFFFFFF`` when ``toId`` alone is inconclusive.

        Returns
        -------
        ClassificationResult
            Immutable classification result with action, category, reason,
            and all metadata fields.
        """
        # --- Extract raw fields ---
        to_id = packet.get("toId", "")
        is_direct = not self._is_broadcast(to_id)
        # Also check numeric `to` field (real meshtastic-python includes both)
        if not is_direct:
            to_numeric = packet.get("to")
            if to_numeric is not None:
                is_direct = not (
                    isinstance(to_numeric, int) and to_numeric == 0xFFFFFFFF
                )

        channel_index = packet.get("channel")
        packet_id = packet.get("id")

        sender_id = packet.get("fromId")
        if sender_id is None:
            from_numeric = packet.get("from")
            if from_numeric is not None:
                sender_id = str(from_numeric)

        is_encrypted = bool(packet.get("encrypted"))

        raw_decoded = packet.get("decoded", {})
        decoded = raw_decoded if isinstance(raw_decoded, dict) else {}
        portnum = normalize_portnum(decoded.get("portnum", None))

        if channel_index is None:
            channel_index = decoded.get("channel")

        # --- Reply / reaction semantics from decoded.replyId and decoded.emoji ---
        reply_id: int | None = None
        if isinstance(decoded, dict):
            reply_id = decoded.get("replyId")
            if reply_id is None:
                reply_id = decoded.get("reply_id")
        emoji_raw = decoded.get("emoji") if isinstance(decoded, dict) else None
        emoji_flag = emoji_raw == EMOJI_FLAG_VALUE

        reaction_key: str | None = None
        if emoji_flag and isinstance(decoded, dict):
            raw_text = decoded.get("text", "")
            if isinstance(raw_text, str):
                stripped = raw_text.strip()
            elif raw_text is not None:
                stripped = str(raw_text).strip()
            else:
                stripped = ""
            reaction_key = stripped if stripped else "?"

        # --- Category determination ---
        is_ack = False
        category: ClassificationCategory = "unknown"
        is_detection_sensor = False
        text_content: str = ""

        if portnum == "detection_sensor":
            is_detection_sensor = True
            category = "unknown"

        if portnum in ("text_message",):
            category = "text"
            text_content = decoded.get("text", "") if isinstance(decoded, dict) else ""
            if text_content is None:
                text_content = ""
        elif portnum in ("text_message_ack",):
            is_ack = True
            category = "ack"
        elif portnum == "routing" and _is_routing_ack(decoded):
            is_ack = True
            category = "ack"
        elif portnum in ("telemetry",):
            category = "telemetry"
        elif portnum in ("nodeinfo",):
            category = "nodeinfo"
        elif portnum in ("position",):
            category = "position"
        elif portnum in ("admin",):
            category = "admin"
        elif portnum and portnum.startswith("plugin_"):
            category = "plugin_only"

        # is_reply / is_reaction only for non-ACK text messages
        is_reply = False
        is_reaction = False
        if not is_ack and category == "text" and reply_id is not None:
            if emoji_flag:
                is_reaction = True
            else:
                is_reply = True

        is_text = category == "text"

        # --- Action decision (classification policy) ---
        action: ClassificationAction
        reason: str

        # 1. Encrypted
        if is_encrypted:
            action = "drop"
            reason = REASON_ENCRYPTED
        # 2. Malformed / no decoded payload
        elif not decoded and not is_encrypted:
            action = "drop"
            reason = REASON_MALFORMED
        # 3. Detection sensor
        elif is_detection_sensor:
            action = "deferred"
            reason = REASON_DETECTION_SENSOR
        # 4. Ack / admin
        elif is_ack or category == "admin":
            action = "ignore"
            reason = REASON_ACK_ADMIN
        # 5. Unknown / custom portnum
        elif category == "unknown":
            action = "deferred"
            reason = REASON_UNKNOWN_PORTNUM
        # 6. Telemetry / position / nodeinfo
        elif category in ("telemetry", "position", "nodeinfo"):
            action = "ignore"
            reason = REASON_NON_CHAT
        # 7. Direct message
        elif is_direct:
            action = "ignore"
            reason = REASON_DIRECT_MESSAGE
        # 8. Plugin-only
        elif category == "plugin_only":
            action = "deferred"
            reason = REASON_PLUGIN_ONLY
        # 9. Empty text
        elif is_text and (not isinstance(text_content, str) or not text_content.strip()):
            action = "ignore"
            reason = REASON_EMPTY_TEXT
        # 10. Text message (relay)
        elif is_text:
            action = "relay"
            reason = REASON_TEXT
        else:
            # Fallback — should not normally be reached
            action = "deferred"
            reason = REASON_UNCLASSIFIED

        routeable = action == "relay"

        return ClassificationResult(
            action=action,
            category=category,
            reason=reason,
            portnum=portnum,
            channel_index=channel_index,
            packet_id=packet_id,
            from_id=sender_id,
            to_id=to_id if isinstance(to_id, str) else str(to_id) if to_id is not None else "",
            is_text=is_text,
            is_ack=is_ack,
            is_encrypted=is_encrypted,
            is_detection_sensor=is_detection_sensor,
            is_direct_message=is_direct,
            routeable=routeable,
            reply_id=reply_id,
            emoji_flag=emoji_flag,
            reaction_key=reaction_key,
            is_reply=is_reply,
            is_reaction=is_reaction,
        )
