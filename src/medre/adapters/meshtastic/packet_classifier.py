"""Meshtastic packet classifier.

:class:`MeshtasticPacketClassifier` examines raw Meshtastic packet dicts
and classifies them by category, direction, and portnum without routing,
publishing, or storage.

The classifier is a pure function: it inspects a packet and returns a
classification dict.  It has no side effects.
"""
from __future__ import annotations

from typing import Any


class MeshtasticPacketClassifier:
    """Classify raw Meshtastic packet dicts.

    Parameters
    ----------
    config:
        Optional :class:`~medre.adapters.meshtastic.config.MeshtasticConfig`
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

            * ``category`` – ``"text"``, ``"telemetry"``, ``"nodeinfo"``,
              ``"position"``, ``"admin"``, ``"unknown"``, or ``"plugin_only"``.
            * ``is_direct_message`` – whether the packet is a DM.
            * ``channel_index`` – radio channel index, or ``None``.
            * ``packet_id`` – packet ID integer, or ``None``.
            * ``sender_id`` – sender node ID, or ``None``.
            * ``portnum`` – decoded portnum string, or ``None``.
            * ``is_ack`` – whether this is an acknowledgement.
        """
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum", None)

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

        if isinstance(portnum, str):
            portnum_lower = portnum.lower()
        elif isinstance(portnum, int):
            # Numeric portnum mapping
            # Tranche-1 scaffold — unverified against real protobuf PortNum enum.
            _NUMERIC_PORTNUM_MAP: dict[int, str] = {
                0: "routing",           # ROUTING_APP
                1: "text_message",      # TEXT_MESSAGE_APP
                2: "text_message_ack",  # TEXT_MESSAGE_ACK_APP
                3: "position",          # POSITION_APP
                4: "nodeinfo",          # NODEINFO_APP
                5: "telemetry",         # TELEMETRY_APP
                6: "store_forward",     # STORE_FORWARD_APP
                7: "waypoint",          # WAYPOINT_APP
                9: "audio",             # AUDIO_APP
                10: "remote_hardware",  # REMOTE_HARDWARE_APP
                11: "private",          # PRIVATE_APP
                68: "paxcounter",       # PAXCOUNTER_APP
                71: "neighbor_info",    # NEIGHBORINFO_APP
                72: "traceroute",       # TRACEROUTE_APP
            }
            portnum_lower = _NUMERIC_PORTNUM_MAP.get(portnum, str(portnum))
            portnum = portnum_lower
        else:
            portnum_lower = ""

        if portnum_lower in ("text_message",):
            category = "text"
        elif portnum_lower in ("text_message_ack",):
            is_ack = True
            category = "text"
        elif portnum_lower in ("telemetry",):
            category = "telemetry"
        elif portnum_lower in ("nodeinfo",):
            category = "nodeinfo"
        elif portnum_lower in ("position",):
            category = "position"
        elif portnum_lower in ("admin",):
            category = "admin"
        elif portnum_lower and portnum_lower.startswith("plugin_"):
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
