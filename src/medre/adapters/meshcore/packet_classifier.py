"""MeshCore packet classifier.

:class:`MeshCorePacketClassifier` examines raw MeshCore event payload dicts
and classifies them by category and direction without routing,
publishing, or storage.

MeshCore packets are simpler than Meshtastic — they carry payload keys
like ``text``, ``pubkey_prefix``, ``channel_idx``, ``type``, ``code``, and
``txt_type`` rather than a portnum enum.

The classifier is a pure function: it inspects a packet and returns a
classification dict.  It has no side effects.
"""
from __future__ import annotations

from typing import Any


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
        for channel mapping lookups (unused in tranche 1).
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config

    def classify(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Classify a raw MeshCore event payload dict.

        Parameters
        ----------
        packet:
            Raw event payload dict with MeshCore-native fields.

        Returns
        -------
        dict
            Classification result with keys:

            * ``category`` – ``"text"``, ``"ack"``, or ``"unknown"``.
            * ``is_direct_message`` – whether the packet is a DM.
            * ``channel_index`` – channel index, or ``None``.
            * ``packet_id`` – sender timestamp integer, or ``None``.
            * ``sender_id`` – pubkey_prefix string, or ``None``.
            * ``is_ack`` – whether this is an acknowledgement.
        """
        text = packet.get("text")
        code = packet.get("code")
        sender_id = packet.get("pubkey_prefix")
        packet_id = packet.get("sender_timestamp")
        channel_index = packet.get("channel_idx")
        msg_type = packet.get("type")

        is_direct = msg_type == "PRIV"
        is_ack = False
        category = "unknown"

        if code is not None:
            is_ack = True
            category = "ack"
        elif text is not None:
            category = "text"

        return {
            "category": category,
            "is_direct_message": is_direct,
            "channel_index": channel_index if not is_direct else None,
            "packet_id": packet_id,
            "sender_id": sender_id,
            "is_ack": is_ack,
        }
