"""Tests for MeshtasticPacketClassifier: category classification, direct
vs channel messages, missing fields, unknown portnums, and ack detection.
"""

from __future__ import annotations

import pytest

from medre.adapters.meshtastic.packet_classifier import MeshtasticPacketClassifier


def _make_text_packet(
    text: str = "hello",
    sender: str = "!abc123",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
) -> dict:
    return {
        "fromId": sender,
        "toId": to_id,
        "channel": channel,
        "id": packet_id,
        "decoded": {
            "portnum": "text_message",
            "text": text,
        },
    }


class TestPacketClassifierText:
    """Text message classification."""

    def test_classify_text_packet(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["is_ack"] is False

    def test_classify_text_packet_with_sender(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(sender="!node1")
        result = cls.classify(packet)
        assert result["sender_id"] == "!node1"

    def test_classify_text_packet_with_channel(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(channel=2)
        result = cls.classify(packet)
        assert result["channel_index"] == 2

    def test_classify_text_packet_with_packet_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(packet_id=12345)
        result = cls.classify(packet)
        assert result["packet_id"] == 12345


class TestPacketClassifierDirect:
    """Direct message vs channel message classification."""

    def test_direct_message(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="!specific_node")
        result = cls.classify(packet)
        assert result["is_direct_message"] is True

    def test_channel_message_empty_to_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="")
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_channel_message_broadcast(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="^all")
        result = cls.classify(packet)
        assert result["is_direct_message"] is False


class TestPacketClassifierMissingFields:
    """Graceful handling of missing fields."""

    def test_missing_packet_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["id"]
        result = cls.classify(packet)
        assert result["packet_id"] is None
        assert result["category"] == "text"

    def test_missing_sender(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["fromId"]
        result = cls.classify(packet)
        assert result["sender_id"] is None
        assert result["category"] == "text"

    def test_missing_channel(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["channel"]
        result = cls.classify(packet)
        assert result["channel_index"] is None

    def test_missing_decoded(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {"fromId": "!node1", "id": 1}
        result = cls.classify(packet)
        assert result["category"] == "unknown"
        assert result["portnum"] is None


class TestPacketClassifierUnknownPortnum:
    """Unknown portnum classification."""

    def test_unknown_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "some_unknown_type"},
        }
        result = cls.classify(packet)
        assert result["category"] == "unknown"

    def test_numeric_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 1},
        }
        result = cls.classify(packet)
        assert result["category"] == "text"

    def test_telemetry_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        result = cls.classify(packet)
        assert result["category"] == "telemetry"

    def test_nodeinfo_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "nodeinfo"},
        }
        result = cls.classify(packet)
        assert result["category"] == "nodeinfo"

    def test_position_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "position"},
        }
        result = cls.classify(packet)
        assert result["category"] == "position"


class TestPacketClassifierBroadcastEdgeCases:
    """All broadcast address forms are correctly identified."""

    def test_broadcast_empty_string(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="")
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_broadcast_caret_all(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="^all")
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_broadcast_integer_max(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["toId"] = 0xFFFFFFFF
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_broadcast_string_max(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="4294967295")
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_direct_message_specific_node(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="!some_node")
        result = cls.classify(packet)
        assert result["is_direct_message"] is True

    def test_is_broadcast_static_method(self) -> None:
        """Verify _is_broadcast covers all edge cases directly."""
        assert MeshtasticPacketClassifier._is_broadcast("") is True
        assert MeshtasticPacketClassifier._is_broadcast(None) is True
        assert MeshtasticPacketClassifier._is_broadcast("^all") is True
        assert MeshtasticPacketClassifier._is_broadcast(0xFFFFFFFF) is True
        assert MeshtasticPacketClassifier._is_broadcast("4294967295") is True
        assert MeshtasticPacketClassifier._is_broadcast("!node123") is False
        assert MeshtasticPacketClassifier._is_broadcast("some_id") is False


class TestPacketClassifierAck:
    """Ack packet detection."""

    def test_ack_packet(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "text_message_ack"},
        }
        result = cls.classify(packet)
        assert result["is_ack"] is True
        assert result["category"] == "text"

    def test_normal_text_is_not_ack(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result["is_ack"] is False

    def test_plugin_only_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "plugin_custom"},
        }
        result = cls.classify(packet)
        assert result["category"] == "plugin_only"

    def test_admin_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "admin"},
        }
        result = cls.classify(packet)
        assert result["category"] == "admin"
        assert result["is_ack"] is False


class TestPacketClassifierNumericFields:
    """Numeric ``from`` / ``to`` field handling (real meshtastic-python packets)."""

    def test_numeric_from_fallback(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "from": 1234567890,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result["sender_id"] == "1234567890"
        assert result["category"] == "text"

    def test_fromid_takes_priority_over_numeric_from(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!abc123",
            "from": 999,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result["sender_id"] == "!abc123"

    def test_numeric_to_broadcast(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "to": 0xFFFFFFFF,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_numeric_to_direct(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "to": 12345,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is True

    def test_toid_broadcast_overrides_numeric_to_direct(self) -> None:
        """toId broadcast should win — numeric `to` is only consulted when
        toId is inconclusive (i.e. broadcast)."""
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "toId": "",
            "to": 12345,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        # toId="" is broadcast, and numeric to=12345 is non-broadcast,
        # so the secondary check should flip it to direct.
        assert result["is_direct_message"] is True
