"""Meshtastic versioned native-metadata contract tests.

The codec emits one ``native.meshtastic`` v1 object. Relation metadata keeps
its established MMRelay/Meshtastic wire keys, while platform detection and
Matrix relay rendering consume only the versioned transport namespace.
"""

from __future__ import annotations

from typing import Any

from medre.adapters._attribution_dispatch import detect_source_platform
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingContext
from tests.helpers.matrix_events import make_meshtastic_event
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig
from tests.helpers.matrix_stubs import StubSourceAttribution as _StubSourceAttribution
from tests.helpers.native_metadata import meshtastic_native_data

# ---------------------------------------------------------------------------
# Packet / config helpers
# ---------------------------------------------------------------------------


def _make_config(adapter_id: str = "mesh-1") -> MeshtasticConfig:
    return MeshtasticConfig(adapter_id=adapter_id)


def _make_text_packet(
    text: str = "hello mesh",
    sender: str = "!node1",
    channel: int = 0,
    packet_id: int = 42,
    to_id: str = "",
) -> dict[str, Any]:
    """Minimal Meshtastic text-message packet dict."""
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


def _native_data(event: CanonicalEvent) -> dict[str, Any]:
    """Return the event's native metadata data dict.

    The codec always constructs a ``NativeMetadata``; this helper narrows
    the optional ``native`` attribute for type safety so tests can index
    into ``data`` without per-line optional access.
    """
    assert event.metadata.native is not None
    data = event.metadata.native.data["meshtastic"]
    assert isinstance(data, dict)
    return data


# ===================================================================
# Group 1: Codec emits one versioned Meshtastic namespace
# ===================================================================


def test_codec_emits_only_meshtastic_native_root() -> None:
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=3, packet_id=77, to_id="!dest"))
    assert event.metadata.native is not None
    assert set(event.metadata.native.data) == {"meshtastic"}
    assert _native_data(event)["schema_version"] == 1


def test_codec_native_fields_match_packet() -> None:
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=5, packet_id=123, to_id="!target"))
    data = _native_data(event)
    assert data["packet_id"] == 123
    assert data["channel"] == 5
    assert data["portnum"] == "text_message"
    assert data["to_id"] == "!target"
    assert data["is_direct_message"] is True


def test_codec_reply_and_emoji_fields() -> None:
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(text="👍", packet_id=300)
    packet["decoded"]["replyId"] = 200
    packet["decoded"]["emoji"] = 1
    data = _native_data(codec.decode(packet))
    assert data["reply_id"] == 200
    assert data["emoji"] == 1
    assert data["emoji_flag"] is True


def test_codec_absent_reply_fields_are_explicit() -> None:
    codec = MeshtasticCodec("mesh-1", _make_config())
    data = _native_data(codec.decode(_make_text_packet()))
    assert data["reply_id"] is None
    assert data["emoji"] is None
    assert data["emoji_flag"] is False


# ===================================================================
# Group 2: SourceNativeRef identical behaviour (channel + packet id)
# ===================================================================


def test_source_native_ref_uses_channel_and_packet_id() -> None:
    """source_native_ref is built from channel + packet id, unaffected by
    namespacing.  native_channel_id and native_message_id match inputs."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=4, packet_id=55))
    ref = event.source_native_ref
    assert ref is not None
    assert ref.adapter == "mesh-1"
    assert ref.native_channel_id == "4"
    assert ref.native_message_id == "55"


def test_source_native_ref_absent_without_packet_id() -> None:
    """No packet id -> no source_native_ref (unchanged behaviour)."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet()
    del packet["id"]
    event = codec.decode(packet)
    assert event.source_native_ref is None


def test_source_native_ref_channel_index_override() -> None:
    """channel_index override flows into source_native_ref.native_channel_id."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(
        _make_text_packet(channel=0, packet_id=1),
        channel_index=8,
    )
    ref = event.source_native_ref
    assert ref is not None
    assert ref.native_channel_id == "8"
    # The namespaced channel metadata also reflects the override.
    assert _native_data(event)["channel"] == 8


# ===================================================================
# Group 3: Reply mapping (relation.metadata wire keys unchanged)
# ===================================================================


def test_reply_relation_keeps_meshtastic_reply_id_wire_key() -> None:
    """A reply relation carries the underscore wire key meshtastic_reply_id
    in relation.metadata — the cross-transport contract is NOT changed by
    non-identity metadata namespacing."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(packet_id=200)
    packet["decoded"]["replyId"] = 100
    event = codec.decode(packet)
    assert len(event.relations) == 1
    rel = event.relations[0]
    assert rel.relation_type == "reply"
    assert rel.metadata.get("meshtastic_reply_id") == "100"
    # Target native ref points at the replied packet id / channel.
    assert rel.target_native_ref is not None
    assert rel.target_native_ref.native_message_id == "100"


def test_reply_relation_no_emoji_wire_key_for_plain_reply() -> None:
    """A plain reply (no emoji flag) does not carry meshtastic_emoji."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(packet_id=200)
    packet["decoded"]["replyId"] = 100
    event = codec.decode(packet)
    rel = event.relations[0]
    assert "meshtastic_emoji" not in rel.metadata


# ===================================================================
# Group 4: Reaction mapping (relation.metadata wire keys unchanged)
# ===================================================================


def test_reaction_relation_keeps_wire_keys() -> None:
    """A reaction (replyId + emoji=1) carries both meshtastic_reply_id and
    meshtastic_emoji underscore wire keys in relation.metadata."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(text="\U0001f44d", packet_id=300)
    packet["decoded"]["replyId"] = 200
    packet["decoded"]["emoji"] = 1
    event = codec.decode(packet)
    assert event.event_kind == "message.reacted"
    assert len(event.relations) == 1
    rel = event.relations[0]
    assert rel.relation_type == "reaction"
    assert rel.metadata.get("meshtastic_reply_id") == "200"
    assert rel.metadata.get("meshtastic_emoji") == 1


def test_reaction_relation_target_ref_uses_reply_id() -> None:
    """The reaction target_native_ref points at reply_id on the packet channel."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(text="\U0001f44d", channel=2, packet_id=300)
    packet["decoded"]["replyId"] = 200
    packet["decoded"]["emoji"] = 1
    event = codec.decode(packet)
    rel = event.relations[0]
    assert rel.target_native_ref is not None
    assert rel.target_native_ref.adapter == "mesh-1"
    assert rel.target_native_ref.native_channel_id == "2"
    assert rel.target_native_ref.native_message_id == "200"


# ===================================================================
# Group 5: Route matching by channel (source_channel_id preserved)
# ===================================================================


def test_source_channel_id_reflects_packet_channel() -> None:
    """source_channel_id (route-match input) is derived from the packet
    channel and is unaffected by native metadata namespacing."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=6))
    assert event.source_channel_id == "6"


def test_source_channel_id_default_channel_fallback() -> None:
    """When the packet lacks a channel, the config default_channel is used
    so source_channel_id still matches routes filtering on source_channel."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet()
    del packet["channel"]
    event = codec.decode(packet)
    # MeshtasticConfig default_channel is 0.
    assert event.source_channel_id == "0"
    assert _native_data(event)["channel"] == 0


# ===================================================================
# Group 6: Unsupported root-level development shapes
# ===================================================================


def test_unversioned_root_fields_do_not_project() -> None:
    from medre.adapters.meshtastic.attribution import project_meshtastic_attribution

    fields = project_meshtastic_attribution(
        {"from_id": "!old", "longname": "Old", "shortname": "OD"},
        source_transport_id=None,
    )
    assert fields["source_sender_id"] is None
    assert fields["source_sender_label"] is None


def test_unversioned_root_fields_do_not_detect_meshtastic() -> None:
    assert (
        detect_source_platform("generic", {"from_id": "!old", "longname": "Old"})
        is None
    )


# ===================================================================
# Group 7: Versioned platform detection
# ===================================================================


def test_versioned_meshtastic_namespace_detected() -> None:
    native = meshtastic_native_data({"packet_id": 42})
    assert detect_source_platform("generic", native) == "meshtastic"


def test_root_channel_alone_not_detected_as_meshtastic() -> None:
    assert detect_source_platform("generic", {"channel": 0}) is None


def test_platform_hint_remains_authoritative() -> None:
    assert (
        detect_source_platform("generic", {"channel": 0}, platform_hint="meshtastic")
        == "meshtastic"
    )


# ===================================================================
# Group 8: Matrix renderer reads meshtastic.packet_id first (paired consumer)
# ===================================================================


def _make_mmrelay_renderer() -> MatrixRenderer:
    """Build a MatrixRenderer whose radio-alpha source has mmrelay_compat."""
    return MatrixRenderer(
        source_configs={
            "radio-alpha": _StubMeshtasticConfig(
                adapter_id="radio-alpha",
                mmrelay_compatibility=True,
            ),
        },
        source_attribution={
            "radio-alpha": _StubSourceAttribution(
                adapter_id="radio-alpha",
                origin_label="AlphaNet",
            ),
        },
        configs={
            "matrix-1": _StubMatrixConfig(
                adapter_id="matrix-1",
                relay_prefix="",
            ),
        },
    )


async def test_matrix_renderer_uses_versioned_packet_id() -> None:
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data=meshtastic_native_data({"packet_id": "111", "longname": "Alice"}),
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == "111"


async def test_matrix_renderer_does_not_read_root_packet_id() -> None:
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={"packet_id": "old-77", "longname": "Alice"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == ""


async def test_matrix_renderer_packet_id_zero_preserved() -> None:
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data=meshtastic_native_data({"packet_id": 0, "longname": "Alice"}),
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == "0"
