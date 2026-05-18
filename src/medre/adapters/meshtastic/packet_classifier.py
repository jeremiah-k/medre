"""Meshtastic packet classifier.

:class:`MeshtasticPacketClassifier` examines raw Meshtastic packet dicts
and classifies them by category, direction, and portnum without routing,
publishing, or storage.

The classifier is a pure function: it inspects a packet and returns a
classification dict.  It has no side effects.
"""

from __future__ import annotations

from typing import Any

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
}

_NORMALIZED_PORTNUMS: set[str] = {
    "text_message",
    "text_message_ack",
    "telemetry",
    "position",
    "nodeinfo",
    "admin",
    "routing",
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
    """Return True for the narrow ROUTING_APP ACK shape used in tranche 1."""
    routing = decoded.get("routing")
    if not isinstance(routing, dict):
        return False
    error_reason = routing.get("errorReason")
    return error_reason == "NONE"


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
        for channel mapping lookups (unused in tranche 1).
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

    def classify(self, packet: dict[str, Any]) -> dict[str, Any]:
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
        dict
            Classification result with keys:

            * ``category`` – ``"text"``, ``"ack"``, ``"telemetry"``,
              ``"nodeinfo"``, ``"position"``, ``"admin"``, ``"unknown"``,
              or ``"plugin_only"``.
            * ``is_direct_message`` – whether the packet is a DM.
            * ``channel_index`` – radio channel index, or ``None``.
            * ``packet_id`` – packet ID integer, or ``None``.
            * ``sender_id`` – sender node ID, or ``None``.
            * ``portnum`` – decoded portnum string, or ``None``.
            * ``is_ack`` – whether this is an acknowledgement.
        """
        raw_decoded = packet.get("decoded", {})
        decoded = raw_decoded if isinstance(raw_decoded, dict) else {}
        portnum = normalize_portnum(decoded.get("portnum", None))

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
        if channel_index is None:
            channel_index = decoded.get("channel")

        packet_id = packet.get("id")

        sender_id = packet.get("fromId")
        if sender_id is None:
            from_numeric = packet.get("from")
            if from_numeric is not None:
                sender_id = str(from_numeric)

        is_ack = False
        category = "unknown"

        if portnum in ("text_message",):
            category = "text"
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
        else:
            category = "unknown"

        return {
            "category": category,
            "is_direct_message": is_direct,
            "channel_index": channel_index,
            "packet_id": packet_id,
            "sender_id": sender_id,
            "portnum": portnum,
            "is_ack": is_ack,
        }
