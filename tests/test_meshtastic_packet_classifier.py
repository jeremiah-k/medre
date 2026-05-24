"""Tests for MeshtasticPacketClassifier: category classification, direct
vs channel messages, missing fields, unknown portnums, ack detection,
action classification (relay/ignore/drop/deferred), and metadata preservation.
"""

from __future__ import annotations

import pytest

from medre.adapters.meshtastic.packet_classifier import (
    MeshtasticPacketClassifier,
    normalize_portnum,
)


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
        assert result.category == "text"
        assert result.is_ack is False

    def test_classify_text_packet_with_sender(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(sender="!node1")
        result = cls.classify(packet)
        assert result.from_id == "!node1"

    def test_classify_text_packet_with_channel(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(channel=2)
        result = cls.classify(packet)
        assert result.channel_index == 2

    def test_classify_text_packet_with_packet_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(packet_id=12345)
        result = cls.classify(packet)
        assert result.packet_id == 12345


class TestPacketClassifierDirect:
    """Direct message vs channel message classification."""

    def test_direct_message(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="!specific_node")
        result = cls.classify(packet)
        assert result.is_direct_message is True

    def test_channel_message_empty_to_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="")
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_channel_message_broadcast(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="^all")
        result = cls.classify(packet)
        assert result.is_direct_message is False


class TestPacketClassifierMissingFields:
    """Graceful handling of missing fields."""

    def test_missing_packet_id(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["id"]
        result = cls.classify(packet)
        assert result.packet_id is None
        assert result.category == "text"

    def test_missing_sender(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["fromId"]
        result = cls.classify(packet)
        assert result.from_id is None
        assert result.category == "text"

    def test_missing_channel(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        del packet["channel"]
        result = cls.classify(packet)
        assert result.channel_index is None

    def test_missing_decoded(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {"fromId": "!node1", "id": 1}
        result = cls.classify(packet)
        assert result.category == "unknown"
        assert result.portnum is None


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
        assert result.category == "unknown"

    def test_numeric_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 1},
        }
        result = cls.classify(packet)
        assert result.category == "text"

    def test_unknown_symbolic_app_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "SOMETHING_ELSE_APP"},
        }
        result = cls.classify(packet)
        assert result.category == "unknown"
        assert result.portnum == "something_else_app"

    def test_telemetry_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        result = cls.classify(packet)
        assert result.category == "telemetry"

    def test_nodeinfo_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "nodeinfo"},
        }
        result = cls.classify(packet)
        assert result.category == "nodeinfo"

    def test_position_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "position"},
        }
        result = cls.classify(packet)
        assert result.category == "position"


class TestPacketClassifierBroadcastEdgeCases:
    """All broadcast address forms are correctly identified."""

    def test_broadcast_empty_string(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="")
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_broadcast_caret_all(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="^all")
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_broadcast_integer_max(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["toId"] = 0xFFFFFFFF
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_broadcast_string_max(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="4294967295")
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_direct_message_specific_node(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(to_id="!some_node")
        result = cls.classify(packet)
        assert result.is_direct_message is True

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
        assert result.is_ack is True
        assert result.category == "ack"

    def test_routing_app_ack_packet(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NONE"},
            },
        }
        result = cls.classify(packet)
        assert result.is_ack is True
        assert result.category == "ack"
        assert result.portnum == "routing"

    def test_routing_app_nak_is_unknown(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NO_CHANNEL"},
            },
        }
        result = cls.classify(packet)
        assert result.is_ack is False
        assert result.category == "unknown"
        assert result.portnum == "routing"

    def test_normal_text_is_not_ack(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.is_ack is False

    def test_plugin_only_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "plugin_custom"},
        }
        result = cls.classify(packet)
        assert result.category == "plugin_only"

    def test_admin_portnum(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "admin"},
        }
        result = cls.classify(packet)
        assert result.category == "admin"
        assert result.is_ack is False


class TestPacketClassifierNumericFields:
    """Numeric ``from`` / ``to`` field handling (real meshtastic-python packets)."""

    def test_numeric_from_fallback(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "from": 1234567890,
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "hi"},
        }
        result = cls.classify(packet)
        assert result.from_id == "1234567890"
        assert result.category == "text"

    def test_fromid_takes_priority_over_numeric_from(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!abc123",
            "from": 999,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result.from_id == "!abc123"

    def test_numeric_to_broadcast(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "to": 0xFFFFFFFF,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is False

    def test_numeric_to_direct(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "to": 12345,
            "id": 1,
            "decoded": {"portnum": "text_message"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is True

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
        assert result.is_direct_message is True


class TestPacketClassifierReplyReaction:
    """reply_id, emoji_flag, reaction_key, is_reply, is_reaction classification."""

    def test_text_with_reply_id_is_reply(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["decoded"]["replyId"] = 100
        result = cls.classify(packet)
        assert result.reply_id == 100
        assert result.emoji_flag is False
        assert result.reaction_key is None
        assert result.is_reply is True
        assert result.is_reaction is False

    def test_text_with_reply_id_and_emoji_is_reaction(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="👍")
        packet["decoded"]["replyId"] = 100
        packet["decoded"]["emoji"] = 1
        result = cls.classify(packet)
        assert result.reply_id == 100
        assert result.emoji_flag is True
        assert result.reaction_key == "👍"
        assert result.is_reply is False
        assert result.is_reaction is True

    def test_reaction_key_empty_text_becomes_question_mark(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="")
        packet["decoded"]["replyId"] = 100
        packet["decoded"]["emoji"] = 1
        result = cls.classify(packet)
        assert result.reaction_key == "?"

    def test_reaction_key_whitespace_text_becomes_question_mark(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="   ")
        packet["decoded"]["replyId"] = 100
        packet["decoded"]["emoji"] = 1
        result = cls.classify(packet)
        assert result.reaction_key == "?"

    def test_text_without_reply_id_no_flags(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.reply_id is None
        assert result.emoji_flag is False
        assert result.reaction_key is None
        assert result.is_reply is False
        assert result.is_reaction is False

    def test_emoji_without_reply_id_not_reaction(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="👍")
        packet["decoded"]["emoji"] = 1
        result = cls.classify(packet)
        assert result.emoji_flag is True
        assert result.reply_id is None
        assert result.is_reaction is False
        assert result.is_reply is False

    def test_ack_with_reply_id_not_reply_not_reaction(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "text_message_ack",
                "replyId": 100,
                "emoji": 1,
            },
        }
        result = cls.classify(packet)
        assert result.is_ack is True
        assert result.is_reply is False
        assert result.is_reaction is False

    def test_non_text_category_not_reply_not_reaction(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "telemetry",
                "replyId": 100,
                "emoji": 1,
            },
        }
        result = cls.classify(packet)
        assert result.is_reply is False
        assert result.is_reaction is False

    def test_emoji_value_not_one_not_flag(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["decoded"]["replyId"] = 100
        packet["decoded"]["emoji"] = 2
        result = cls.classify(packet)
        assert result.emoji_flag is False
        assert result.is_reply is True
        assert result.is_reaction is False


class TestPortnumNormalization:
    """Real symbolic Meshtastic portnum normalization."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("TEXT_MESSAGE_APP", "text_message"),
            ("TELEMETRY_APP", "telemetry"),
            ("POSITION_APP", "position"),
            ("NODEINFO_APP", "nodeinfo"),
            ("ADMIN_APP", "admin"),
            ("ROUTING_APP", "routing"),
            ("TEXT_MESSAGE_ACK_APP", "text_message_ack"),
            ("DETECTION_SENSOR_APP", "detection_sensor"),
            ("text_message", "text_message"),
            ("telemetry", "telemetry"),
            (1, "text_message"),
            (None, None),
            ("UNKNOWN_FUTURE_APP", "unknown_future_app"),
        ],
    )
    def test_normalize_portnum(self, raw, expected) -> None:
        assert normalize_portnum(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "category"),
        [
            ("TEXT_MESSAGE_APP", "text"),
            ("TELEMETRY_APP", "telemetry"),
            ("POSITION_APP", "position"),
            ("NODEINFO_APP", "nodeinfo"),
            ("ADMIN_APP", "admin"),
        ],
    )
    def test_real_symbolic_app_classification(self, raw, category) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": raw, "text": "hello"},
        }
        result = cls.classify(packet)
        assert result.category == category


class TestClassifierReplyIdZero:
    """Classifier handles replyId=0 deterministically."""

    def test_reply_id_zero_without_emoji(self) -> None:
        """replyId=0 without emoji: is_reply True, is_reaction False."""
        packet = _make_text_packet(text="hello")
        packet["decoded"]["replyId"] = 0
        result = MeshtasticPacketClassifier().classify(packet)
        assert result.reply_id == 0
        assert result.is_reply is True
        assert result.is_reaction is False

    def test_reply_id_zero_with_emoji(self) -> None:
        """replyId=0 with emoji=1: is_reaction True, is_reply False."""
        packet = _make_text_packet(text="👍")
        packet["decoded"]["replyId"] = 0
        packet["decoded"]["emoji"] = 1
        result = MeshtasticPacketClassifier().classify(packet)
        assert result.reply_id == 0
        assert result.emoji_flag is True
        assert result.is_reaction is True
        assert result.is_reply is False
        assert result.reaction_key == "👍"

    def test_reply_id_zero_with_emoji_empty_text(self) -> None:
        """replyId=0 + emoji=1 with empty text: reaction_key is '?'."""
        packet = _make_text_packet(text="")
        packet["decoded"]["replyId"] = 0
        packet["decoded"]["emoji"] = 1
        result = MeshtasticPacketClassifier().classify(packet)
        assert result.reaction_key == "?"
        assert result.is_reaction is True

    def test_non_string_text_does_not_throw(self) -> None:
        """Non-string decoded text with emoji does not raise."""
        packet = _make_text_packet(text="irrelevant")
        packet["decoded"]["text"] = 42
        packet["decoded"]["replyId"] = 5
        packet["decoded"]["emoji"] = 1
        result = MeshtasticPacketClassifier().classify(packet)
        # Should not throw. reaction_key should be str(42)
        assert result.reaction_key == "42"


# ===================================================================
# Classification action tests
# ===================================================================


class TestClassificationActionTests:
    """Verify classification action (relay/ignore/drop/deferred) and reason."""

    def test_text_classified_as_relay(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="hello world")
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.is_text is True
        assert result.routeable is True

    def test_ack_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "text_message_ack"},
        }
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "ack/admin/system message"
        assert result.is_ack is True

    def test_malformed_no_decoded_classified_as_drop(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {"fromId": "!node1", "id": 1}
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.reason == "malformed or missing decoded payload"

    def test_encrypted_classified_as_drop(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "encrypted": True,
            "decoded": {"portnum": "text_message", "text": "secret"},
        }
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.reason == "encrypted packet"
        assert result.is_encrypted is True

    def test_detection_sensor_classified_as_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "detection_sensor"},
        }
        result = cls.classify(packet)
        assert result.action == "deferred"
        assert result.reason == "detection sensor packets are deferred"
        assert result.is_detection_sensor is True

    def test_detection_sensor_symbolic_app_classified_as_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "DETECTION_SENSOR_APP"},
        }
        result = cls.classify(packet)
        assert result.action == "deferred"
        assert result.is_detection_sensor is True

    def test_unknown_portnum_classified_as_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "some_unknown_type"},
        }
        result = cls.classify(packet)
        assert result.action == "deferred"
        assert result.reason == "unknown or custom portnum"

    def test_telemetry_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "telemetry"},
        }
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "non-chat message type"

    def test_position_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "position"},
        }
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "non-chat message type"

    def test_nodeinfo_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "nodeinfo"},
        }
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "non-chat message type"

    def test_direct_message_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="private msg", to_id="!target_node")
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "direct message to specific node"
        assert result.is_direct_message is True

    def test_plugin_only_classified_as_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "plugin_custom"},
        }
        result = cls.classify(packet)
        assert result.action == "deferred"
        assert result.reason == "plugin_only packets are deferred"

    def test_empty_text_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="")
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "empty text"
        assert result.routeable is False

    def test_whitespace_only_text_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="   ")
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "empty text"
        assert result.routeable is False

    def test_normal_text_routeable(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="hello world")
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.routeable is True

    def test_encrypted_routeable_false(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "encrypted": True,
            "decoded": {"portnum": "text_message", "text": "secret"},
        }
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.routeable is False

    def test_direct_message_routeable_false(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="dm", to_id="!target")
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.routeable is False

    def test_admin_classified_as_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": "admin"},
        }
        result = cls.classify(packet)
        assert result.action == "ignore"
        assert result.reason == "ack/admin/system message"

    def test_relay_result_includes_all_metadata(self) -> None:
        """A relay result preserves all metadata fields."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(
            text="hello", sender="!sender1", channel=3, packet_id=99
        )
        packet["decoded"]["replyId"] = 55
        packet["decoded"]["emoji"] = 1
        result = cls.classify(packet)

        assert result.action == "relay"
        assert result.category == "text"
        assert result.reason == "text message"
        assert result.portnum == "text_message"
        assert result.channel_index == 3
        assert result.packet_id == 99
        assert result.from_id == "!sender1"
        assert result.to_id == ""
        assert result.is_text is True
        assert result.is_ack is False
        assert result.is_encrypted is False
        assert result.is_detection_sensor is False
        assert result.is_direct_message is False
        assert result.routeable is True
        assert result.reply_id == 55
        assert result.emoji_flag is True
        assert result.reaction_key == "hello"
        assert result.is_reply is False
        assert result.is_reaction is True

    def test_classification_result_is_frozen(self) -> None:
        """ClassificationResult is immutable."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        with pytest.raises(AttributeError):
            result.action = "drop"  # type: ignore[misc]

    def test_encrypted_text_is_not_routeable(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "encrypted": True,
            "decoded": {"portnum": "text_message", "text": "secret"},
        }
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"
        assert result.routeable is False

    def test_dm_text_is_not_routeable(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="dm", to_id="!target")
        result = cls.classify(packet)
        assert result.is_direct_message is True
        assert result.action == "ignore"
        assert result.routeable is False
