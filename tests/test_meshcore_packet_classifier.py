"""Tests for MeshCorePacketClassifier: category classification, direct
vs channel messages, missing fields, ACK detection, and edge cases.
"""

from __future__ import annotations

from medre.adapters.meshcore.packet_classifier import MeshCorePacketClassifier


class TestPacketClassifierText:
    """Text message classification."""

    def test_classify_contact_text_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 42,
            "type": "PRIV",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["is_ack"] is False

    def test_classify_channel_text_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello channel",
            "channel_idx": 2,
            "sender_timestamp": 100,
            "type": "CHAN",
            "txt_type": 0,
            "pubkey_prefix": "chan_sender",
        }
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["is_ack"] is False

    def test_classify_text_packet_with_sender(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "node1",
            "sender_timestamp": 42,
            "type": "PRIV",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["sender_id"] == "node1"

    def test_classify_text_packet_with_packet_id(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 12345,
            "type": "CHAN",
            "channel_idx": 0,
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["packet_id"] == 12345


class TestPacketClassifierDirect:
    """Direct message vs channel message classification."""

    def test_direct_message_priv(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "dm",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 42,
            "type": "PRIV",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is True

    def test_channel_message_chan(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "channel msg",
            "channel_idx": 0,
            "sender_timestamp": 42,
            "type": "CHAN",
            "txt_type": 0,
            "pubkey_prefix": "sender",
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is False

    def test_channel_index_none_for_dm(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "dm",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 42,
            "type": "PRIV",
            "txt_type": 0,
            "channel_idx": 5,
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is True
        assert result["channel_index"] is None

    def test_channel_index_set_for_channel(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "channel msg",
            "channel_idx": 3,
            "sender_timestamp": 42,
            "type": "CHAN",
            "txt_type": 0,
            "pubkey_prefix": "sender",
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is False
        assert result["channel_index"] == 3


class TestPacketClassifierMissingFields:
    """Graceful handling of missing fields."""

    def test_missing_packet_id(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "type": "PRIV",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["packet_id"] is None
        assert result["category"] == "text"

    def test_missing_sender(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "sender_timestamp": 42,
            "type": "CHAN",
            "channel_idx": 0,
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["sender_id"] is None
        assert result["category"] == "text"

    def test_missing_channel_idx(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 42,
            "type": "CHAN",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["channel_index"] is None

    def test_empty_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        result = cls.classify({})
        assert result["category"] == "unknown"
        assert result["is_ack"] is False
        assert result["sender_id"] is None
        assert result["packet_id"] is None


class TestPacketClassifierAck:
    """ACK packet detection."""

    def test_ack_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"code": 0}
        result = cls.classify(packet)
        assert result["is_ack"] is True
        assert result["category"] == "ack"

    def test_ack_packet_with_nonzero_code(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"code": 1}
        result = cls.classify(packet)
        assert result["is_ack"] is True
        assert result["category"] == "ack"

    def test_normal_text_is_not_ack(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 42,
            "type": "PRIV",
            "txt_type": 0,
        }
        result = cls.classify(packet)
        assert result["is_ack"] is False
        assert result["category"] == "text"


class TestPacketClassifierUnknown:
    """Unknown packet classification."""

    def test_packet_with_only_type(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"type": "UNKNOWN"}
        result = cls.classify(packet)
        assert result["category"] == "unknown"

    def test_packet_with_unrelated_fields(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"foo": "bar"}
        result = cls.classify(packet)
        assert result["category"] == "unknown"
