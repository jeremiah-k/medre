"""Tests for MeshCoreCodec: decode (native → canonical), metadata
population, source_native_ref, and edge cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.errors import MeshCoreCodecError
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind


def _meshcore_data(event: CanonicalEvent) -> dict[str, object]:
    """Return the current versioned meshcore native namespace."""
    assert event.metadata.native is not None
    data = event.metadata.native.data["meshcore"]
    assert isinstance(data, dict)
    return data


def _make_config() -> MeshCoreConfig:
    return MeshCoreConfig(adapter_id="meshcore-1")


def _make_contact_packet(
    text: str = "hello meshcore",
    sender: str = "abc123",
    timestamp: int = 42,
    txt_type: int = 0,
) -> dict:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": txt_type,
    }


def _make_channel_packet(
    text: str = "hello channel",
    channel_idx: int = 0,
    timestamp: int = 42,
    txt_type: int = 0,
    sender: str = "chan_sender",
) -> dict:
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": sender,
    }


class TestMeshCoreCodecDecode:
    """MeshCoreCodec decode behaviour."""

    def test_decode_contact_text_message(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(text="hello meshcore")
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.event_kind == EventKind.MESSAGE_CREATED
        assert event.payload["body"] == "hello meshcore"

    def test_decode_channel_text_message(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(text="hello channel")
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)
        assert event.payload["body"] == "hello channel"

    def test_decode_sets_source_adapter(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        event = codec.decode(packet)
        assert event.source_adapter == "meshcore-1"

    def test_decode_sets_source_transport_id(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(sender="node1")
        event = codec.decode(packet)
        assert event.source_transport_id == "node1"

    def test_decode_channel_sets_source_channel_id(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(channel_idx=2)
        event = codec.decode(packet)
        assert event.source_channel_id == "2"

    def test_decode_contact_dm_no_channel_id(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        event = codec.decode(packet)
        assert event.source_channel_id is None

    def test_decode_channel_extracts_wire_sender_name_as_label(self) -> None:
        """Group texts carry the sender's node name on the wire.

        MeshCore's firmware prepends the sending node's advertised name to
        every group text ("<name>: <text>") because CHANNEL_MSG_RECV
        carries no sender identity in the protocol.  The codec must lift
        that name into ``contact_label`` so relay attribution (e.g.
        Matrix "{sender}/{origin_label}: ") renders the sender instead of
        an empty field (user-observed "/medre-lab: ..." in the live room).
        """
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(text="MEDRE-MC-B: hello from b")
        event = codec.decode(packet)
        assert _meshcore_data(event)["contact_label"] == "MEDRE-MC-B"
        # Body stays verbatim — wire fidelity is pinned by the live pair
        # suite; only the attribution label is derived.
        assert event.payload["body"] == "MEDRE-MC-B: hello from b"

    def test_decode_channel_without_wire_name_keeps_label_none(self) -> None:
        """Texts that do not follow the "<name>: " convention stay None."""
        codec = MeshCoreCodec("meshcore-1", _make_config())
        event = codec.decode(_make_channel_packet(text="just talking"))
        assert _meshcore_data(event)["contact_label"] is None

    def test_decode_contact_dm_does_not_extract_wire_name(self) -> None:
        """DM packets carry real identity; the wire convention does not
        apply and a "name-looking" text must not become a label."""
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(text="MEDRE-MC-B: hello dm")
        event = codec.decode(packet)
        assert _meshcore_data(event)["contact_label"] is None

    def test_decode_explicit_contact_label_wins_over_wire_name(self) -> None:
        """A resolved known-contact label takes precedence over the
        wire-embedded name for channel packets."""
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(text="MEDRE-MC-B: hello")
        event = codec.decode(packet, contact_label="Known Contact")
        assert _meshcore_data(event)["contact_label"] == "Known Contact"

    def test_decode_missing_text_graceful(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        del packet["text"]
        with pytest.raises(MeshCoreCodecError, match="unsupported"):
            codec.decode(packet)

    def test_decode_missing_packet_id(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        del packet["sender_timestamp"]
        event = codec.decode(packet)
        assert event.source_native_ref is None

    def test_decode_missing_sender(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        del packet["pubkey_prefix"]
        event = codec.decode(packet)
        assert event.source_transport_id == ""

    def test_decode_non_dict_raises(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet: Any = "not a dict"
        with pytest.raises(MeshCoreCodecError, match="dict"):
            codec.decode(packet)

    def test_decode_ack_raises(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = {"code": 0}
        with pytest.raises(MeshCoreCodecError, match="ACK"):
            codec.decode(packet)

    def test_decode_populates_native_metadata(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(sender="node1", timestamp=99)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        data = _meshcore_data(event)
        assert data["packet_id"] == 99
        assert data["sender_id"] == "node1"
        assert data["channel"] is None

    def test_decode_channel_metadata(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(channel_idx=3, timestamp=100)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        data = _meshcore_data(event)
        assert data["channel"] == 3
        assert data["packet_id"] == 100

    def test_decode_dm_metadata(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert _meshcore_data(event)["is_direct_message"] is True

    def test_decode_channel_metadata_not_direct(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet()
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert _meshcore_data(event)["is_direct_message"] is False

    def test_decode_stores_pubkey_prefix(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(sender="deadbeef")
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert _meshcore_data(event)["pubkey_prefix"] == "deadbeef"

    def test_decode_stores_txt_type(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(txt_type=1)
        event = codec.decode(packet)
        assert event.metadata.native is not None
        assert _meshcore_data(event)["txt_type"] == 1

    def test_decode_no_reply_relations(self) -> None:
        """MeshCore has no native replyId — relations should always be empty."""
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        event = codec.decode(packet)
        assert len(event.relations) == 0


class TestMeshCoreCodecSourceNativeRef:
    """source_native_ref carries the MEDRE-derived message identity.

    MeshCore provides no native message ID; the codec derives a
    deterministic identity digest over the identity-bearing fields
    (sender, channel, sender_timestamp, txt_type, text, direct flag).
    """

    def test_decode_populates_source_native_ref(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(timestamp=12345)
        event = codec.decode(packet)
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "meshcore-1"
        assert event.source_native_ref.native_channel_id == "0"
        # The durable id is the derived identity, never the bare
        # sender_timestamp (two same-second messages would collide).
        mid = event.source_native_ref.native_message_id
        assert mid != "12345"
        assert mid.startswith("mc1-")
        assert len(mid) == len("mc1-") + 64

    def test_decode_contact_dm_native_ref_no_channel(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet(timestamp=9999)
        event = codec.decode(packet)
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "meshcore-1"
        assert event.source_native_ref.native_channel_id is None
        assert event.source_native_ref.native_message_id.startswith("mc1-")

    def test_decode_empty_packet_id_no_ref(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_contact_packet()
        del packet["sender_timestamp"]
        event = codec.decode(packet)
        assert event.source_native_ref is None

    def test_decode_channel_index_override(self) -> None:
        codec = MeshCoreCodec("meshcore-1", _make_config())
        packet = _make_channel_packet(channel_idx=0, timestamp=1)
        event = codec.decode(packet, channel_index=5)
        assert event.source_channel_id == "5"
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_channel_id == "5"


def test_same_timestamp_distinct_text_distinct_ids() -> None:
    codec = MeshCoreCodec("meshcore-1", _make_config())
    first = codec.decode(_make_channel_packet(text="a", timestamp=700))
    second = codec.decode(_make_channel_packet(text="b", timestamp=700))
    assert first.source_native_ref is not None
    assert second.source_native_ref is not None
    assert (
        first.source_native_ref.native_message_id
        != second.source_native_ref.native_message_id
    )


def test_identity_deterministic_across_codec_instances() -> None:
    first = MeshCoreCodec("meshcore-1", _make_config()).decode(
        _make_channel_packet(timestamp=700)
    )
    second = MeshCoreCodec("meshcore-1", _make_config()).decode(
        _make_channel_packet(timestamp=700)
    )
    assert first.source_native_ref is not None
    assert second.source_native_ref is not None
    assert (
        first.source_native_ref.native_message_id
        == second.source_native_ref.native_message_id
    )
