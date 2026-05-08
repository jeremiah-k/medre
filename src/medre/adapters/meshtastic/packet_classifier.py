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

    def classify(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Classify a raw Meshtastic packet dict.

        Parameters
        ----------
        packet:
            Raw packet dict with Meshtastic-native fields.

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
        # In Meshtastic, broadcast is "" or "^all" or 0xffffffff
        is_direct = bool(to_id) and to_id not in ("^all",)

        channel_index = packet.get("channel")
        if channel_index is None:
            channel_index = decoded.get("channel")

        packet_id = packet.get("id")

        sender_id = packet.get("fromId")

        is_ack = False
        category = "unknown"

        if isinstance(portnum, str):
            portnum_lower = portnum.lower()
        elif isinstance(portnum, int):
            # Numeric portnum mapping
            _NUMERIC_PORTNUM_MAP: dict[int, str] = {
                1: "text_message",
                2: "text_message_ack",
                3: "position",
                4: "nodeinfo",
                5: "telemetry",
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
