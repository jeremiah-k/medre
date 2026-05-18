"""Tests for MeshtasticCodec: decode (native → canonical), metadata
population, source_native_ref, replyId relation extraction, and edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.errors import MeshtasticCodecError
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind


def _make_config() -> MeshtasticConfig:
    return MeshtasticConfig(adapter_id="mesh-1")


def _make_text_packet(
    text: str = "hello mesh",
    sender: str = "!node1",
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


class TestMeshtasticCodecDecode:
    """MeshtasticCodec decode behaviour."""

    def test_decode_text_message(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(text="hello mesh")
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.event_kind == EventKind.MESSAGE_CREATED
        assert event.payload["body"] == "hello mesh"

    def test_decode_sets_source_adapter(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert event.source_adapter == "mesh-1"

    def test_decode_sets_source_transport_id(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(sender="!node1")
        event = codec.decode(packet)
        assert event.source_transport_id == "!node1"

    def test_decode_sets_source_channel_id(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(channel=2)
        event = codec.decode(packet)
        assert event.source_channel_id == "2"

    def test_decode_missing_text_graceful(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        del packet["decoded"]["text"]
        event = codec.decode(packet)
        assert event.payload["body"] == ""

    def test_decode_missing_packet_id(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        del packet["id"]
        event = codec.decode(packet)
        assert event.source_native_ref is None

    def test_decode_missing_sender(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        del packet["fromId"]
        event = codec.decode(packet)
        assert event.source_transport_id == ""

    def test_decode_missing_decoded(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = {"id": 1, "channel": 0}
        with pytest.raises(MeshtasticCodecError, match="unsupported"):
            codec.decode(packet)

    def test_decode_non_dict_raises(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet: Any = "not a dict"
        with pytest.raises(MeshtasticCodecError, match="dict"):
            codec.decode(packet)

    def test_decode_populates_native_metadata(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(sender="!node1", packet_id=99)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        data = event.metadata.native.data
        assert data["packet_id"] == 99
        assert data["from_id"] == "!node1"
        assert data["channel"] == 0

    def test_decode_broadcast_metadata(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(to_id="")
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["is_direct_message"] is False
        assert event.metadata.native.data["to_id"] == ""

    def test_decode_direct_message_metadata(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(to_id="!target_node")
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["is_direct_message"] is True
        assert event.metadata.native.data["to_id"] == "!target_node"

    def test_decode_dm_vs_channel_distinguishable(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        dm_packet = _make_text_packet(to_id="!specific")
        ch_packet = _make_text_packet(to_id="")
        dm_event = codec.decode(dm_packet)
        ch_event = codec.decode(ch_packet)
        assert dm_event.metadata.native is not None
        assert ch_event.metadata.native is not None
        assert dm_event.metadata.native.data["is_direct_message"] is True
        assert ch_event.metadata.native.data["is_direct_message"] is False

    def test_decode_text_message_app_symbolic_portnum(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(text="real symbolic")
        packet["decoded"]["portnum"] = "TEXT_MESSAGE_APP"
        event = codec.decode(packet)
        assert event.payload["body"] == "real symbolic"
        assert event.payload["portnum"] == "text_message"
        assert event.metadata.native is not None
        assert event.metadata.native.data["portnum"] == "text_message"

    @pytest.mark.parametrize(
        "portnum",
        ["TELEMETRY_APP", "ADMIN_APP", "POSITION_APP", "NODEINFO_APP"],
    )
    def test_decode_rejects_unsupported_symbolic_portnums(self, portnum) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(text="not text")
        packet["decoded"]["portnum"] = portnum
        with pytest.raises(MeshtasticCodecError, match="unsupported"):
            codec.decode(packet)

    def test_decode_rejects_routing_ack(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": "ROUTING_APP",
                "routing": {"errorReason": "NONE"},
            },
        }
        with pytest.raises(MeshtasticCodecError, match="ACK"):
            codec.decode(packet)

    def test_decode_numeric_to_metadata_matches_classifier(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(to_id="")
        packet["to"] = 12345
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert event.metadata.native.data["is_direct_message"] is True

    def test_decode_numeric_sender_fallback_matches_classifier(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        del packet["fromId"]
        packet["from"] = 123456
        event = codec.decode(packet)
        assert event.source_transport_id == "123456"
        assert event.metadata.native is not None
        assert event.metadata.native.data["from_id"] == "123456"


class TestMeshtasticCodecSourceNativeRef:
    """source_native_ref population from packet ID."""

    def test_decode_populates_source_native_ref(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(packet_id=12345)
        event = codec.decode(packet)
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "mesh-1"
        assert event.source_native_ref.native_channel_id == "0"
        assert event.source_native_ref.native_message_id == "12345"

    def test_decode_empty_packet_id_no_ref(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        del packet["id"]
        event = codec.decode(packet)
        assert event.source_native_ref is None

    def test_decode_channel_index_override(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(channel=0, packet_id=1)
        event = codec.decode(packet, channel_index=5)
        assert event.source_channel_id == "5"
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_channel_id == "5"


class TestMeshtasticCodecReplyRelation:
    """replyId extraction into EventRelation."""

    def test_decode_reply_id_creates_relation(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(packet_id=200)
        packet["decoded"]["replyId"] = 100
        event = codec.decode(packet)

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reply"
        assert rel.target_event_id is None
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "100"
        assert rel.target_native_ref.adapter == "mesh-1"

    def test_decode_no_reply_id_no_relation(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet()
        event = codec.decode(packet)
        assert len(event.relations) == 0

    def test_decode_reply_preserves_source_native_ref(self) -> None:
        codec = MeshtasticCodec("mesh-1", _make_config())
        packet = _make_text_packet(packet_id=200)
        packet["decoded"]["replyId"] = 100
        event = codec.decode(packet)

        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "200"
        assert len(event.relations) == 1
