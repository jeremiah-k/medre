"""Tests for MeshtasticPacketClassifier: classification actions, numeric
portnum resolution, channel mapping semantics, field extraction (hop, rx_time,
priority, radio, via_mqtt), encrypted hardening, broadcast numeric, and
self-echo suppression.
"""

from __future__ import annotations

import pytest

import medre.adapters.meshtastic.packet_classifier as _classifier_mod
from medre.adapters.meshtastic.packet_classifier import (
    REASON_SELF_ECHO,
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


# ===================================================================
# Numeric portnum resolution tests
# ===================================================================


class TestNumericPortnumFallback:
    """Protocol-correct numeric portnum resolution without the SDK."""

    @pytest.mark.parametrize(
        ("numeric", "expected"),
        [
            (0, "unknown"),
            (1, "text_message"),
            (2, "remote_hardware"),
            (3, "position"),
            (4, "nodeinfo"),
            (5, "routing"),
            (6, "admin"),
            (9, "audio"),
            (10, "detection_sensor"),
            (34, "paxcounter"),
            (67, "telemetry"),
            (71, "neighborinfo"),
            (72, "atak_plugin"),
        ],
    )
    def test_fallback_values_no_sdk(self, numeric, expected, monkeypatch) -> None:
        """Fallback map used when SDK table is unavailable."""
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(_classifier_mod, "_get_sdk_portnum_table", lambda: None)
        assert normalize_portnum(numeric) == expected

    def test_sdk_values_stripped_app_suffix(self, monkeypatch) -> None:
        """SDK values with _app suffix are stripped for MEDRE consistency."""
        custom_table = {
            2: "remote_hardware_app",
            9: "audio_app",
            34: "paxcounter_app",
            71: "neighborinfo_app",
        }
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(
            _classifier_mod, "_get_sdk_portnum_table", lambda: custom_table
        )
        assert normalize_portnum(2) == "remote_hardware"
        assert normalize_portnum(9) == "audio"
        assert normalize_portnum(34) == "paxcounter"
        assert normalize_portnum(71) == "neighborinfo"

    def test_sdk_values_without_app_suffix_preserved(self, monkeypatch) -> None:
        """SDK values without _app suffix are returned as-is."""
        custom_table = {1: "text_message", 5: "routing"}
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(
            _classifier_mod, "_get_sdk_portnum_table", lambda: custom_table
        )
        assert normalize_portnum(1) == "text_message"
        assert normalize_portnum(5) == "routing"

    def test_unknown_numeric_returns_string(self) -> None:
        assert normalize_portnum(999) == "999"

    def test_negative_numeric_returns_string(self) -> None:
        assert normalize_portnum(-1) == "-1"


class TestNumericPortnumClassification:
    """Classification of packets with numeric portnum values."""

    def test_numeric_0_unknown_is_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 0},
        }
        result = cls.classify(packet)
        assert result.portnum == "unknown"
        assert result.category == "unknown"
        assert result.action == "deferred"
        assert result.reason == "unknown or custom portnum"

    def test_numeric_5_routing_ack(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": 5,
                "routing": {"errorReason": "NONE"},
            },
        }
        result = cls.classify(packet)
        assert result.portnum == "routing"
        assert result.is_ack is True
        assert result.category == "ack"
        assert result.action == "ignore"

    def test_numeric_5_routing_non_ack(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {
                "portnum": 5,
                "routing": {"errorReason": "NO_CHANNEL"},
            },
        }
        result = cls.classify(packet)
        assert result.portnum == "routing"
        assert result.is_ack is False
        assert result.category == "unknown"
        assert result.action == "deferred"

    def test_numeric_6_admin_is_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 6},
        }
        result = cls.classify(packet)
        assert result.portnum == "admin"
        assert result.category == "admin"
        assert result.action == "ignore"
        assert result.reason == "ack/admin/system message"

    def test_numeric_10_detection_sensor_is_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 10},
        }
        result = cls.classify(packet)
        assert result.portnum == "detection_sensor"
        assert result.is_detection_sensor is True
        assert result.action == "deferred"
        assert result.reason == "detection sensor packets are deferred"

    def test_numeric_67_telemetry_is_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 67},
        }
        result = cls.classify(packet)
        assert result.portnum == "telemetry"
        assert result.category == "telemetry"
        assert result.action == "ignore"
        assert result.reason == "non-chat message type"

    def test_numeric_999_unknown_is_deferred(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "decoded": {"portnum": 999},
        }
        result = cls.classify(packet)
        assert result.portnum == "999"
        assert result.category == "unknown"
        assert result.action == "deferred"
        assert result.reason == "unknown or custom portnum"

    def test_numeric_portnum_with_encrypted_is_drop(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "id": 1,
            "encrypted": True,
            "decoded": {"portnum": 1, "text": "secret"},
        }
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"
        assert result.reason == "encrypted packet"

    def test_numeric_portnum_direct_message_is_ignore(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "toId": "!target_node",
            "id": 1,
            "decoded": {"portnum": 1, "text": "private numeric"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is True
        assert result.action == "ignore"
        assert result.reason == "direct message to specific node"

    def test_numeric_portnum_no_decoded_is_drop(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = {"fromId": "!node1", "id": 1}
        result = cls.classify(packet)
        assert result.action == "drop"
        assert result.reason == "malformed or missing decoded payload"


class TestNumericPortnumSdkOverride:
    """SDK table override via monkeypatch."""

    def test_sdk_table_overrides_fallback(self, monkeypatch) -> None:
        """When SDK table returns a custom mapping, it takes precedence."""
        custom_table = {42: "custom_app_from_sdk"}
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(
            _classifier_mod, "_get_sdk_portnum_table", lambda: custom_table
        )
        # "custom_app_from_sdk" does NOT end with "_app", returned as-is
        assert normalize_portnum(42) == "custom_app_from_sdk"

    def test_sdk_table_none_uses_fallback(self, monkeypatch) -> None:
        """When SDK table is None, fallback map is used."""
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(_classifier_mod, "_get_sdk_portnum_table", lambda: None)
        assert normalize_portnum(1) == "text_message"
        assert normalize_portnum(10) == "detection_sensor"

    def test_sdk_table_partial_coverage_falls_back(self, monkeypatch) -> None:
        """SDK table only has some values; unknown-to-SDK falls back correctly."""
        custom_table = {42: "custom_app"}
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_FETCHED", False)
        monkeypatch.setattr(_classifier_mod, "_SDK_PORTNUM_CACHE", None)
        monkeypatch.setattr(
            _classifier_mod, "_get_sdk_portnum_table", lambda: custom_table
        )
        # 42 is in SDK table; "custom_app" ends with _app → stripped to "custom"
        assert normalize_portnum(42) == "custom"
        # 5 is NOT in SDK table, falls back to protocol-correct map
        assert normalize_portnum(5) == "routing"


# ===================================================================
# channel_mapping semantics tests
# ===================================================================


class TestChannelMappingSemantics:
    """channel_mapping is labeling-only — unmapped channels classify normally.

    These tests prove that the packet classifier does NOT gate on
    ``config.channel_mapping``.  A text packet on an unmapped channel
    index still receives ``action="relay"`` with ``reason="text message"``.
    """

    def test_unmapped_channel_relayed_without_config(self) -> None:
        """No config at all — unmapped channel 7 text is relayed."""
        cls = MeshtasticPacketClassifier(config=None)
        packet = _make_text_packet(text="hello", channel=7)
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.channel_index == 7
        assert result.routeable is True

    def test_unmapped_channel_relayed_with_empty_mapping(self) -> None:
        """Config with empty channel_mapping — unmapped channel 5 is relayed."""
        from medre.config.adapters.meshtastic import MeshtasticConfig

        cfg = MeshtasticConfig(adapter_id="test", channel_mapping={})
        cls = MeshtasticPacketClassifier(config=cfg)
        packet = _make_text_packet(text="hello", channel=5)
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.channel_index == 5
        assert result.routeable is True

    def test_unmapped_channel_relayed_with_partial_mapping(self) -> None:
        """Config maps channel 0 only — channel 3 (unmapped) is still relayed."""
        from medre.config.adapters.meshtastic import MeshtasticConfig

        cfg = MeshtasticConfig(adapter_id="test", channel_mapping={0: "general"})
        cls = MeshtasticPacketClassifier(config=cfg)
        packet = _make_text_packet(text="hello", channel=3)
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.channel_index == 3
        assert result.routeable is True

    def test_mapped_channel_also_relayed(self) -> None:
        """A mapped channel is also relayed — mapping does not change behavior."""
        from medre.config.adapters.meshtastic import MeshtasticConfig

        cfg = MeshtasticConfig(
            adapter_id="test", channel_mapping={0: "general", 1: "admin"}
        )
        cls = MeshtasticPacketClassifier(config=cfg)
        packet = _make_text_packet(text="hello", channel=0)
        result = cls.classify(packet)
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.channel_index == 0
        assert result.routeable is True


# ===================================================================
# Hardened field extraction tests (source audit gap closure)
# ===================================================================


class TestHopStartHopLimit:
    """hop_start / hop_limit extraction from real mtjk packet fields."""

    def test_hop_start_present(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["hopStart"] = 3
        result = cls.classify(packet)
        assert result.hop_start == 3
        assert result.hop_limit is None

    def test_hop_limit_present(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["hopLimit"] = 5
        result = cls.classify(packet)
        assert result.hop_start is None
        assert result.hop_limit == 5

    def test_both_hop_fields(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["hopStart"] = 3
        packet["hopLimit"] = 5
        result = cls.classify(packet)
        assert result.hop_start == 3
        assert result.hop_limit == 5

    def test_hop_fields_absent(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.hop_start is None
        assert result.hop_limit is None

    def test_hop_fields_zero(self) -> None:
        """Zero is a valid hop value — extract it."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["hopStart"] = 0
        packet["hopLimit"] = 0
        result = cls.classify(packet)
        assert result.hop_start == 0
        assert result.hop_limit == 0


class TestRxTimeExtraction:
    """rx_time extraction via extract_meshtastic_rx_time."""

    def test_valid_rx_time(self) -> None:
        from datetime import datetime, timezone

        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxTime"] = 1700000000
        result = cls.classify(packet)
        assert result.rx_time is not None
        assert result.rx_time == datetime.fromtimestamp(1700000000, tz=timezone.utc)

    def test_missing_rx_time(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.rx_time is None

    def test_invalid_rx_time_zero(self) -> None:
        """rxTime=0 is rejected by extract_meshtastic_rx_time (non-positive)."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxTime"] = 0
        result = cls.classify(packet)
        assert result.rx_time is None

    def test_invalid_rx_time_negative(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxTime"] = -100
        result = cls.classify(packet)
        assert result.rx_time is None

    def test_invalid_rx_time_string(self) -> None:
        """Non-numeric rxTime is rejected."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxTime"] = "not-a-timestamp"
        result = cls.classify(packet)
        assert result.rx_time is None

    def test_invalid_rx_time_bool(self) -> None:
        """Bool rxTime is rejected (bool is subclass of int)."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxTime"] = True
        result = cls.classify(packet)
        assert result.rx_time is None


class TestPriorityExtraction:
    """priority field extraction (protobuf MeshPacket.Priority enum name)."""

    def test_priority_present(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["priority"] = "RELIABLE"
        result = cls.classify(packet)
        assert result.priority == "RELIABLE"

    def test_priority_absent(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.priority is None

    def test_priority_empty_string(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["priority"] = ""
        result = cls.classify(packet)
        assert result.priority == ""

    def test_priority_numeric(self) -> None:
        """priority=3 (integer enum value) → stored as string '3'."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["priority"] = 3
        result = cls.classify(packet)
        assert result.priority == "3"


class TestRxSnrRxRssi:
    """rx_snr / rx_rssi radio diagnostic field extraction."""

    def test_rx_snr_present(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxSnr"] = 7.5
        result = cls.classify(packet)
        assert result.rx_snr == 7.5
        assert result.rx_rssi is None

    def test_rx_rssi_present(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxRssi"] = -80
        result = cls.classify(packet)
        assert result.rx_snr is None
        assert result.rx_rssi == -80

    def test_both_radio_fields(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxSnr"] = 5.25
        packet["rxRssi"] = -90
        result = cls.classify(packet)
        assert result.rx_snr == 5.25
        assert result.rx_rssi == -90

    def test_radio_fields_absent(self) -> None:
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.rx_snr is None
        assert result.rx_rssi is None

    def test_rx_snr_zero(self) -> None:
        """Zero SNR is a valid measurement."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxSnr"] = 0.0
        result = cls.classify(packet)
        assert result.rx_snr == 0.0

    def test_rx_snr_negative(self) -> None:
        """Negative rx_snr (valid — signal below noise floor)."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["rxSnr"] = -5.5
        result = cls.classify(packet)
        assert result.rx_snr == -5.5


class TestViaMqtt:
    """via_mqtt diagnostic field extraction."""

    def test_via_mqtt_true(self) -> None:
        """viaMqtt=True → via_mqtt is True."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["viaMqtt"] = True
        result = cls.classify(packet)
        assert result.via_mqtt is True

    def test_via_mqtt_false(self) -> None:
        """viaMqtt=False → via_mqtt is False."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["viaMqtt"] = False
        result = cls.classify(packet)
        assert result.via_mqtt is False

    def test_via_mqtt_absent(self) -> None:
        """No viaMqtt field → via_mqtt defaults to False."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.via_mqtt is False


class TestEncryptedHardening:
    """Encrypted packet detection hardened for real mtjk protobuf bool."""

    def test_encrypted_python_bool_true(self) -> None:
        """encrypted=True (Python bool) triggers drop."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["encrypted"] = True
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"
        assert result.reason == "encrypted packet"

    def test_encrypted_truthy_int(self) -> None:
        """encrypted=1 (truthy int) also triggers drop."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["encrypted"] = 1
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"

    def test_encrypted_false_no_drop(self) -> None:
        """encrypted=False does not trigger drop."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["encrypted"] = False
        result = cls.classify(packet)
        assert result.is_encrypted is False
        assert result.action == "relay"

    def test_encrypted_decoded_level_fallback(self) -> None:
        """encrypted inside decoded dict triggers drop when top-level absent."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["decoded"]["encrypted"] = True
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"
        assert result.reason == "encrypted packet"

    def test_encrypted_both_levels_top_wins(self) -> None:
        """Top-level encrypted=True takes precedence over decoded.encrypted=False."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["encrypted"] = True
        packet["decoded"]["encrypted"] = False
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"

    def test_encrypted_neither_level(self) -> None:
        """No encrypted at either level — not flagged."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.is_encrypted is False

    def test_encrypted_false_top_true_decoded(self) -> None:
        """encrypted=False at top level + encrypted=True in decoded → is_encrypted is True."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["encrypted"] = False
        packet["decoded"]["encrypted"] = True
        result = cls.classify(packet)
        assert result.is_encrypted is True
        assert result.action == "drop"


class TestBroadcastNumericToInt:
    """to=0xFFFFFFFF (int) correctly identified as broadcast via numeric path."""

    def test_numeric_to_broadcast_no_toid(self) -> None:
        """Packet with to=0xFFFFFFFF and no toId is broadcast."""
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "to": 0xFFFFFFFF,
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "hello"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is False
        assert result.action == "relay"

    def test_numeric_to_broadcast_with_empty_toid(self) -> None:
        """Packet with to=0xFFFFFFFF and toId="" is broadcast."""
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "toId": "",
            "to": 0xFFFFFFFF,
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "hello"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is False
        assert result.action == "relay"

    def test_string_4294967295_toid_is_broadcast(self) -> None:
        """toId='4294967295' (string form of 0xFFFFFFFF) is broadcast."""
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "toId": "4294967295",
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "hello"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is False
        assert result.action == "relay"

    def test_numeric_to_direct_message(self) -> None:
        """to=12345 (non-broadcast int) with broadcast toId flips to direct."""
        cls = MeshtasticPacketClassifier()
        packet = {
            "fromId": "!node1",
            "toId": "",
            "to": 12345,
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "hello"},
        }
        result = cls.classify(packet)
        assert result.is_direct_message is True
        assert result.action == "ignore"


class TestAllNewFieldsTogether:
    """Full packet with all new fields extracted correctly."""

    def test_full_mtjk_packet(self) -> None:
        from datetime import datetime, timezone

        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="full packet", sender="!abc123")
        packet["hopStart"] = 3
        packet["hopLimit"] = 7
        packet["rxTime"] = 1700000000
        packet["priority"] = "HIGH"
        packet["rxSnr"] = 8.25
        packet["rxRssi"] = -65
        result = cls.classify(packet)

        assert result.action == "relay"
        assert result.hop_start == 3
        assert result.hop_limit == 7
        assert result.rx_time == datetime.fromtimestamp(1700000000, tz=timezone.utc)
        assert result.priority == "HIGH"
        assert result.rx_snr == 8.25
        assert result.rx_rssi == -65
        assert result.via_mqtt is False

    def test_full_mtjk_packet_defaults(self) -> None:
        """Packet without any new fields — all default to None."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)

        assert result.hop_start is None
        assert result.hop_limit is None
        assert result.rx_time is None
        assert result.priority is None
        assert result.rx_snr is None
        assert result.rx_rssi is None
        assert result.via_mqtt is False

    def test_result_still_frozen_with_new_fields(self) -> None:
        """ClassificationResult remains immutable with new fields."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        packet["hopStart"] = 2
        result = cls.classify(packet)
        with pytest.raises(AttributeError):
            result.hop_start = 99  # type: ignore[misc]


# ===================================================================
# Self-echo suppression tests
# ===================================================================


class TestSelfEchoSuppression:
    """Packets from our own node are detected and ignored to prevent loops."""

    def test_self_echo_detected(self) -> None:
        """Packet with fromId == own_node_id is classified as self-echo."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="echo", sender="!abc123", to_id="")
        result = cls.classify(packet, own_node_id="!abc123")
        assert result.is_self_echo is True
        assert result.action == "ignore"
        assert result.reason == REASON_SELF_ECHO
        assert result.routeable is False

    def test_no_self_echo_for_other_sender(self) -> None:
        """Packet from a different node is NOT self-echo."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="hello", sender="!other999", to_id="")
        result = cls.classify(packet, own_node_id="!abc123")
        assert result.is_self_echo is False
        assert result.action == "relay"
        assert result.reason == "text message"
        assert result.routeable is True

    def test_no_self_echo_when_own_node_id_unknown(self) -> None:
        """classify() with own_node_id=None (default) never flags self-echo."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="hello", sender="!abc123", to_id="")
        # Default: own_node_id not passed
        result = cls.classify(packet)
        assert result.is_self_echo is False
        assert result.action == "relay"

        # Explicit None
        result2 = cls.classify(packet, own_node_id=None)
        assert result2.is_self_echo is False
        assert result2.action == "relay"

    def test_self_echo_from_numeric_from(self) -> None:
        """When SDK didn't populate fromId (node not in nodesByNum),
        classifier derives sender_id from numeric `from` field using
        canonical "!{num:08x}" format to match own_node_id.
        """
        cls = MeshtasticPacketClassifier()
        packet = {
            "from": 12345,  # 0x3039
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "echo"},
        }
        result = cls.classify(packet, own_node_id="!00003039")
        assert result.is_self_echo is True
        assert result.action == "ignore"
        assert result.reason == REASON_SELF_ECHO

    def test_self_echo_when_fromId_present(self) -> None:
        """Normal case: SDK populates fromId in "!{num:08x}" format.
        Self-echo detected by direct string match with own_node_id.
        """
        cls = MeshtasticPacketClassifier()
        packet = {
            "from": 12345,
            "fromId": "!00003039",
            "id": 1,
            "decoded": {"portnum": "text_message", "text": "echo"},
        }
        result = cls.classify(packet, own_node_id="!00003039")
        assert result.is_self_echo is True

    def test_self_echo_takes_priority_over_encrypted(self) -> None:
        """Self-echo check runs before encrypted check."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet(text="secret echo", sender="!abc123")
        packet["encrypted"] = True
        result = cls.classify(packet, own_node_id="!abc123")
        assert result.is_self_echo is True
        assert result.action == "ignore"
        assert result.reason == REASON_SELF_ECHO

    def test_self_echo_default_false_without_param(self) -> None:
        """Existing callers without own_node_id get is_self_echo=False."""
        cls = MeshtasticPacketClassifier()
        packet = _make_text_packet()
        result = cls.classify(packet)
        assert result.is_self_echo is False
