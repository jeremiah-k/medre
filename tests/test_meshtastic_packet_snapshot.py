"""Tests for Meshtastic packet snapshot helpers."""

from __future__ import annotations

import json

import msgspec

from medre.adapters.meshtastic.packet_snapshot import (
    json_safe,
    snapshot_decoded,
    snapshot_packet,
)


class TestJsonSafe:
    """json_safe conversion rules."""

    def test_none(self) -> None:
        assert json_safe(None) is None

    def test_bool(self) -> None:
        assert json_safe(True) is True
        assert json_safe(False) is False

    def test_int(self) -> None:
        assert json_safe(42) == 42

    def test_float(self) -> None:
        assert json_safe(3.14) == 3.14

    def test_str(self) -> None:
        assert json_safe("hello") == "hello"

    def test_bytes_to_base64(self) -> None:
        raw = b"\x89PNG\r\n"
        result = json_safe(raw)
        assert isinstance(result, dict)
        assert result["encoding"] == "base64"
        assert isinstance(result["data"], str)
        # Verify round-trip
        import base64
        assert base64.b64decode(result["data"]) == raw

    def test_bytearray_to_base64(self) -> None:
        raw = bytearray(b"\x00\x01\x02")
        result = json_safe(raw)
        assert isinstance(result, dict)
        assert result["encoding"] == "base64"

    def test_dict_recursive(self) -> None:
        data = {"key": b"\xff", "nested": {"inner": 42}}
        result = json_safe(data)
        assert isinstance(result["key"], dict)
        assert result["key"]["encoding"] == "base64"
        assert result["nested"]["inner"] == 42

    def test_list_recursive(self) -> None:
        data = [b"\x00", "hello", 42]
        result = json_safe(data)
        assert isinstance(result, list)
        assert isinstance(result[0], dict)
        assert result[1] == "hello"
        assert result[2] == 42

    def test_tuple_to_list(self) -> None:
        result = json_safe((1, 2, 3))
        assert isinstance(result, list)
        assert result == [1, 2, 3]

    def test_unknown_object_uses_repr(self) -> None:
        class Custom:
            def __repr__(self) -> str:
                return "CustomObj()"
        result = json_safe(Custom())
        assert isinstance(result, str)
        assert result == "CustomObj()"


class TestJsonSafeSerializable:
    """json_safe output is msgspec/json serializable."""

    def test_json_round_trip(self) -> None:
        data = {
            "text": "hello",
            "binary": b"\x89PNG",
            "count": 42,
            "flag": True,
            "nothing": None,
            "list": [1, b"\x00", "x"],
        }
        safe = json_safe(data)
        encoded = json.dumps(safe)
        decoded = json.loads(encoded)
        assert decoded["text"] == "hello"
        assert decoded["binary"]["encoding"] == "base64"

    def test_msgspec_encode(self) -> None:
        data = {"key": b"\xff", "val": 42}
        safe = json_safe(data)
        encoded = msgspec.json.encode(safe)
        decoded = msgspec.json.decode(encoded)
        assert decoded["val"] == 42


class TestSnapshotDecoded:
    """snapshot_decoded snapshots the decoded sub-dict."""

    def test_simple_decoded(self) -> None:
        decoded = {"portnum": "text_message", "text": "hello"}
        result = snapshot_decoded(decoded)
        assert result["portnum"] == "text_message"
        assert result["text"] == "hello"

    def test_decoded_with_bytes(self) -> None:
        decoded = {"portnum": "text_message", "payload": b"\x00\x01"}
        result = snapshot_decoded(decoded)
        assert isinstance(result["payload"], dict)
        assert result["payload"]["encoding"] == "base64"

    def test_non_dict_input(self) -> None:
        result = snapshot_decoded("not a dict")
        # str passes through json_safe unchanged
        assert result == "not a dict"

    def test_non_dict_non_string_input(self) -> None:
        result = snapshot_decoded(42)
        assert result == 42


class TestSnapshotPacket:
    """snapshot_packet snapshots a full packet dict."""

    def test_full_packet(self) -> None:
        packet = {
            "fromId": "!node1",
            "toId": "",
            "id": 42,
            "channel": 0,
            "decoded": {
                "portnum": "text_message",
                "text": "hello",
            },
        }
        result = snapshot_packet(packet)
        assert result["fromId"] == "!node1"
        assert result["decoded"]["text"] == "hello"

    def test_packet_with_bytes_payload(self) -> None:
        packet = {
            "id": 1,
            "decoded": {"payload": b"\x00\xff"},
        }
        result = snapshot_packet(packet)
        assert isinstance(result["decoded"]["payload"], dict)
        assert result["decoded"]["payload"]["encoding"] == "base64"

    def test_non_dict_input(self) -> None:
        result = snapshot_packet(None)
        assert result is None

    def test_snapshot_is_msgspec_serializable(self) -> None:
        packet = {
            "id": 1,
            "decoded": {"portnum": "text_message", "bytes": b"\x89"},
        }
        result = snapshot_packet(packet)
        encoded = msgspec.json.encode(result)
        assert isinstance(encoded, bytes)
