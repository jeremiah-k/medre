"""Tests for MeshCorePacketClassifier: category classification, direct
vs channel messages, missing fields, ACK detection, and edge cases.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass

import pytest

from medre.adapters.meshcore.packet_classifier import (
    ClassificationResult,
    MeshCorePacketClassifier,
)


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
        assert isinstance(result, ClassificationResult)
        assert result.category == "direct_message"
        assert result.is_ack is False
        assert result.is_text is True
        assert result.action == "relay"
        assert result.routeable is True
        assert result.reason == "direct_text_packet"

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
        assert result.category == "text"
        assert result.is_ack is False
        assert result.is_text is True
        assert result.action == "relay"
        assert result.routeable is True
        assert result.reason == "channel_text_packet"

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
        assert result.sender_id == "node1"

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
        assert result.packet_id == 12345


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
        assert result.is_direct_message is True
        assert result.action == "relay"
        assert result.routeable is True

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
        assert result.is_direct_message is False

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
        assert result.is_direct_message is True
        assert result.channel_index is None

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
        assert result.is_direct_message is False
        assert result.channel_index == 3


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
        assert result.packet_id is None
        assert result.category == "direct_message"
        assert result.action == "relay"

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
        assert result.sender_id is None
        assert result.category == "text"
        assert result.action == "relay"

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
        assert result.channel_index is None

    def test_empty_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        result = cls.classify({})
        assert result.category == "malformed"
        assert result.is_ack is False
        assert result.sender_id is None
        assert result.packet_id is None
        assert result.action == "drop"
        assert result.routeable is False
        assert result.reason == "malformed_packet"


class TestPacketClassifierAck:
    """ACK packet detection."""

    def test_ack_packet(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"code": 0}
        result = cls.classify(packet)
        assert result.is_ack is True
        assert result.category == "ack"
        assert result.action == "ignore"
        assert result.routeable is False
        assert result.reason == "ack_packet"

    def test_ack_packet_with_nonzero_code(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"code": 1}
        result = cls.classify(packet)
        assert result.is_ack is True
        assert result.category == "ack"
        assert result.action == "ignore"

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
        assert result.is_ack is False
        assert result.category == "direct_message"
        assert result.action == "relay"


class TestPacketClassifierUnknown:
    """Unknown packet classification."""

    def test_packet_with_only_type(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"type": "UNKNOWN"}
        result = cls.classify(packet)
        assert result.category == "unknown"
        assert result.action == "deferred"
        assert result.reason == "unknown_packet"

    def test_packet_with_unrelated_fields(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"foo": "bar"}
        result = cls.classify(packet)
        assert result.category == "malformed"
        assert result.action == "drop"
        assert result.reason == "malformed_packet"


class TestPacketClassifierEmptyText:
    """Empty/whitespace-only text is ignored (not relayed)."""

    def test_empty_string_text_channel(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "", "type": "CHAN", "channel_idx": 0}
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.category == "text"
        assert result.reason == "empty_text_packet"
        assert result.routeable is False
        assert result.is_text is True

    def test_whitespace_only_text_channel(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "   \t\n  ", "type": "CHAN", "channel_idx": 0}
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.category == "text"
        assert result.reason == "empty_text_packet"
        assert result.routeable is False

    def test_empty_string_text_dm(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "", "type": "PRIV", "pubkey_prefix": "abc"}
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.category == "direct_message"
        assert result.reason == "empty_text_packet"
        assert result.routeable is False
        assert result.is_text is True

    def test_whitespace_only_text_dm(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "  ", "type": "PRIV", "pubkey_prefix": "abc"}
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.category == "direct_message"
        assert result.reason == "empty_text_packet"
        assert result.routeable is False


class TestPacketClassifierMalformed:
    """PRIV/CHAN without text are malformed (drop)."""

    def test_priv_no_text(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"type": "PRIV", "pubkey_prefix": "abc", "sender_timestamp": 42}
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.category == "malformed"
        assert result.reason == "malformed_packet"
        assert result.routeable is False

    def test_chan_no_text(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"type": "CHAN", "channel_idx": 0, "sender_timestamp": 42}
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.category == "malformed"
        assert result.reason == "malformed_packet"
        assert result.routeable is False

    def test_text_no_type_relays(self) -> None:
        """Text present but no recognised type — treat as generic text, relay."""
        cls = MeshCorePacketClassifier()
        packet = {"text": "hello", "sender_timestamp": 1}
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.category == "text"
        assert result.reason == "channel_text_packet"
        assert result.routeable is True


class TestClassificationResultFrozen:
    """ClassificationResult is frozen/immutable."""

    def test_result_is_frozen(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "frozen test", "type": "CHAN", "txt_type": 0}
        result = cls.classify(packet)
        assert is_dataclass(result)
        assert result.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
        with pytest.raises(FrozenInstanceError):
            result.action = "drop"  # type: ignore[misc]

    def test_result_has_all_fields(self) -> None:
        cls = MeshCorePacketClassifier()
        packet = {"text": "fields test", "type": "CHAN", "txt_type": 0}
        result = cls.classify(packet)
        expected_fields = {
            "action",
            "category",
            "reason",
            "channel_index",
            "packet_id",
            "sender_id",
            "is_direct_message",
            "is_ack",
            "is_text",
            "routeable",
        }
        actual_fields = {f.name for f in fields(result)}
        assert actual_fields == expected_fields
