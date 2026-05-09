"""Tests for LxmfPacketClassifier: category classification, sender/ID
extraction, has_fields, direct message detection, and edge cases.
"""

from __future__ import annotations

import pytest

from medre.adapters.lxmf.errors import LxmfCodecError
from medre.adapters.lxmf.packet_classifier import (
    LxmfPacketClassifier,
    normalize_lxmf_text,
)


class TestPacketClassifierText:
    """Text message classification."""

    def test_classify_text_packet(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
            "timestamp": 1700000000.0,
        }
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["is_ack"] is False

    def test_classify_text_with_title(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "body text",
            "title": "Subject",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["category"] == "text"

    def test_classify_text_with_sender(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["sender_id"] == "ab" * 16

    def test_classify_text_with_packet_id(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["packet_id"] == "cd" * 32

    def test_classify_text_bytes_source_hash(self) -> None:
        """source_hash as bytes should be normalised to hex string."""
        cls = LxmfPacketClassifier()
        raw_hash = bytes.fromhex("ab" * 16)
        packet = {
            "content": "hello",
            "source_hash": raw_hash,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["sender_id"] == "ab" * 16

    def test_classify_text_bytes_message_id(self) -> None:
        """message_id as bytes should be normalised to hex string."""
        cls = LxmfPacketClassifier()
        raw_id = bytes.fromhex("cd" * 32)
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
            "message_id": raw_id,
        }
        result = cls.classify(packet)
        assert result["packet_id"] == "cd" * 32


class TestPacketClassifierDirectMessage:
    """LXMF DM detection — always True."""

    def test_is_direct_message_always_true(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "dm",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["is_direct_message"] is True

    def test_channel_index_always_none(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "msg",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["channel_index"] is None


class TestPacketClassifierHasFields:
    """has_fields detection."""

    def test_has_fields_true(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "fields test",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
            "fields": {0xFD: "data"},
        }
        result = cls.classify(packet)
        assert result["has_fields"] is True

    def test_has_fields_false_empty_dict(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "no fields",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
            "fields": {},
        }
        result = cls.classify(packet)
        assert result["has_fields"] is False

    def test_has_fields_false_none(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "no fields",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
            "fields": None,
        }
        result = cls.classify(packet)
        assert result["has_fields"] is False

    def test_has_fields_false_missing(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "no fields",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["has_fields"] is False


class TestPacketClassifierMissingFields:
    """Graceful handling of missing fields."""

    def test_missing_packet_id(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
        }
        result = cls.classify(packet)
        assert result["packet_id"] is None
        assert result["category"] == "text"

    def test_missing_sender(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "content": "hello",
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["sender_id"] is None
        assert result["category"] == "text"

    def test_empty_packet(self) -> None:
        cls = LxmfPacketClassifier()
        result = cls.classify({})
        assert result["category"] == "unknown"
        assert result["is_ack"] is False
        assert result["sender_id"] is None
        assert result["packet_id"] is None


class TestPacketClassifierUnsupported:
    """Unsupported (attachment-only) packet classification."""

    def test_attachment_only_is_unsupported(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
            "fields": {0x05: [{"name": "file.txt", "size": 100}]},
        }
        result = cls.classify(packet)
        assert result["category"] == "unsupported"

    def test_content_none_with_fields_is_unsupported(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {
            "source_hash": "ab" * 16,
            "content": None,
            "fields": {0x06: "image data"},
        }
        result = cls.classify(packet)
        assert result["category"] == "unsupported"


class TestPacketClassifierUnknown:
    """Unknown packet classification."""

    def test_packet_with_unrelated_fields(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {"foo": "bar"}
        result = cls.classify(packet)
        assert result["category"] == "unknown"

    def test_packet_with_only_source_hash(self) -> None:
        cls = LxmfPacketClassifier()
        packet = {"source_hash": "ab" * 16}
        result = cls.classify(packet)
        assert result["category"] == "unknown"


class TestPacketClassifierBytesContent:
    """Bytes content/title normalisation."""

    def test_classify_bytes_content(self) -> None:
        """Bytes content is classified as 'text' and normalised."""
        cls = LxmfPacketClassifier()
        packet = {
            "content": b"hello bytes",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["content"] == "hello bytes"

    def test_classify_bytes_title(self) -> None:
        """Bytes title is normalised to str."""
        cls = LxmfPacketClassifier()
        packet = {
            "content": "body",
            "title": b"Subject",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["title"] == "Subject"

    def test_classify_str_content(self) -> None:
        """Str content still works unchanged."""
        cls = LxmfPacketClassifier()
        packet = {
            "content": "plain string",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        result = cls.classify(packet)
        assert result["category"] == "text"
        assert result["content"] == "plain string"

    def test_classify_none_content(self) -> None:
        """None content → unknown category, empty string in result."""
        cls = LxmfPacketClassifier()
        packet = {
            "content": None,
            "source_hash": "ab" * 16,
        }
        result = cls.classify(packet)
        assert result["category"] == "unknown"
        assert result["content"] == ""

    def test_classify_invalid_content_type(self) -> None:
        """Int content raises LxmfCodecError."""
        cls = LxmfPacketClassifier()
        packet = {
            "content": 42,
            "source_hash": "ab" * 16,
        }
        with pytest.raises(LxmfCodecError, match="unsupported content type"):
            cls.classify(packet)


class TestNormalizeLxmfText:
    """Direct tests for the normalize_lxmf_text helper."""

    def test_normalize_lxmf_text_str(self) -> None:
        assert normalize_lxmf_text("hello") == "hello"

    def test_normalize_lxmf_text_bytes(self) -> None:
        assert normalize_lxmf_text(b"hello bytes") == "hello bytes"

    def test_normalize_lxmf_text_bytearray(self) -> None:
        assert normalize_lxmf_text(bytearray(b"hello ba")) == "hello ba"

    def test_normalize_lxmf_text_none(self) -> None:
        assert normalize_lxmf_text(None) == ""

    def test_normalize_lxmf_text_invalid_utf8(self) -> None:
        with pytest.raises(LxmfCodecError, match="invalid UTF-8"):
            normalize_lxmf_text(b"\xff\xfe\x00\x01")

    def test_normalize_lxmf_text_unsupported_type(self) -> None:
        with pytest.raises(LxmfCodecError, match="unsupported content type"):
            normalize_lxmf_text(123)
