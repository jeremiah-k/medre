"""Meshtastic non-identity packet metadata namespacing tests.

Validates that :class:`~medre.adapters.meshtastic.codec.MeshtasticCodec`
emits namespaced ``meshtastic.*`` equivalents for non-identity packet
metadata alongside the retained bare keys (legacy stored-event tolerance
and current non-identity consumers), and that the paired Matrix renderer
consumer reads the namespaced ``meshtastic.packet_id`` first with bare
``packet_id`` legacy fallback.

Namespaced keys asserted (all emitted alongside their bare counterparts):

* ``meshtastic.packet_id``
* ``meshtastic.channel``
* ``meshtastic.portnum``
* ``meshtastic.to_id``
* ``meshtastic.reply_id``
* ``meshtastic.emoji``
* ``meshtastic.emoji_flag``
* ``meshtastic.is_direct_message``

Preserved behaviour (unchanged by namespacing):

* route matching by ``source_channel`` (``source_channel_id`` derives from
  the packet channel, independent of native metadata keys);
* ``source_native_ref`` construction (channel + packet id);
* reply/reaction relation mapping (relation.metadata underscore wire keys
  ``meshtastic_reply_id`` / ``meshtastic_emoji`` are a separate
  cross-transport contract and are NOT changed);
* renderer relation fallback;
* platform detection is NOT triggered by bare ``channel`` alone (bare
  ``channel`` is too generic), while namespaced ``meshtastic.channel`` IS
  an unambiguous Meshtastic-native signal.
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
    return event.metadata.native.data


# Namespaced non-identity keys that must be emitted by the codec.
_NAMESPACED_NON_IDENTITY_KEYS = (
    "meshtastic.packet_id",
    "meshtastic.channel",
    "meshtastic.portnum",
    "meshtastic.to_id",
    "meshtastic.reply_id",
    "meshtastic.emoji",
    "meshtastic.emoji_flag",
    "meshtastic.is_direct_message",
)

# Pairs of (bare key, namespaced key) that must hold identical values.
_BARE_TO_NAMESPACED = (
    ("packet_id", "meshtastic.packet_id"),
    ("channel", "meshtastic.channel"),
    ("portnum", "meshtastic.portnum"),
    ("to_id", "meshtastic.to_id"),
    ("reply_id", "meshtastic.reply_id"),
    ("emoji", "meshtastic.emoji"),
    ("emoji_flag", "meshtastic.emoji_flag"),
    ("is_direct_message", "meshtastic.is_direct_message"),
)


# ===================================================================
# Group 1: Codec emits namespaced non-identity metadata
# ===================================================================


def test_codec_emits_all_namespaced_non_identity_keys() -> None:
    """Every required namespaced non-identity key is present in native data."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(channel=3, packet_id=77, to_id="!dest")
    event = codec.decode(packet)
    assert event.metadata.native is not None
    data = _native_data(event)
    for key in _NAMESPACED_NON_IDENTITY_KEYS:
        assert key in data, f"missing namespaced key: {key}"


def test_codec_namespaced_keys_equal_bare_keys() -> None:
    """Each namespaced key carries the same value as its bare counterpart."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(channel=5, packet_id=123, to_id="!target")
    event = codec.decode(packet)
    data = _native_data(event)
    for bare, namespaced in _BARE_TO_NAMESPACED:
        assert (
            data[bare] == data[namespaced]
        ), f"{bare}={data[bare]!r} != {namespaced}={data[namespaced]!r}"


def test_codec_namespaced_packet_id_matches_input() -> None:
    """meshtastic.packet_id reflects the packet id (int preserved)."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(packet_id=9001))
    data = _native_data(event)
    assert data["meshtastic.packet_id"] == 9001


def test_codec_namespaced_channel_matches_input() -> None:
    """meshtastic.channel reflects the packet channel index."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=7))
    data = _native_data(event)
    assert data["meshtastic.channel"] == 7


def test_codec_namespaced_portnum_value() -> None:
    """meshtastic.portnum carries the normalized portnum string."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet())
    data = _native_data(event)
    assert data["meshtastic.portnum"] == "text_message"
    assert data["meshtastic.portnum"] == data["portnum"]


def test_codec_namespaced_to_id_and_dm() -> None:
    """meshtastic.to_id and meshtastic.is_direct_message track the packet."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    dm = codec.decode(_make_text_packet(to_id="!specific"))
    dm_data = _native_data(dm)
    assert dm_data["meshtastic.to_id"] == "!specific"
    assert dm_data["meshtastic.is_direct_message"] is True

    broadcast = codec.decode(_make_text_packet(to_id=""))
    b_data = _native_data(broadcast)
    assert b_data["meshtastic.to_id"] == ""
    assert b_data["meshtastic.is_direct_message"] is False


def test_codec_namespaced_reply_and_emoji_fields() -> None:
    """meshtastic.reply_id / .emoji / .emoji_flag populated for a tapback."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    packet = _make_text_packet(text="\U0001f44d", packet_id=300)
    packet["decoded"]["replyId"] = 200
    packet["decoded"]["emoji"] = 1
    event = codec.decode(packet)
    data = _native_data(event)
    assert data["meshtastic.reply_id"] == 200
    assert data["meshtastic.emoji"] == 1
    assert data["meshtastic.emoji_flag"] is True
    # Bare equivalents retained and identical.
    assert data["reply_id"] == 200
    assert data["emoji"] == 1
    assert data["emoji_flag"] is True


def test_codec_namespaced_keys_absent_when_no_value() -> None:
    """A plain broadcast text packet has None for namespaced reply/emoji."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet())
    data = _native_data(event)
    assert data["meshtastic.reply_id"] is None
    assert data["meshtastic.emoji"] is None
    assert data["meshtastic.emoji_flag"] is False


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
    assert _native_data(event)["meshtastic.channel"] == 8


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
    assert _native_data(event)["meshtastic.channel"] == 0


# ===================================================================
# Group 6: Legacy bare-key fixture tolerance
# ===================================================================


def test_codec_still_emits_bare_non_identity_keys() -> None:
    """Bare non-identity keys are retained alongside namespaced keys for
    legacy stored-event tolerance and current consumers."""
    codec = MeshtasticCodec("mesh-1", _make_config())
    event = codec.decode(_make_text_packet(channel=1, packet_id=9))
    data = _native_data(event)
    for bare, _namespaced in _BARE_TO_NAMESPACED:
        assert bare in data, f"missing bare key: {bare}"


def test_projection_reads_bare_from_id_legacy_fixture() -> None:
    """project_meshtastic_attribution still accepts bare from_id/longname/
    shortname for legacy fixtures that carry no namespaced keys."""
    from medre.adapters.meshtastic.attribution import (
        project_meshtastic_attribution,
    )

    fields = project_meshtastic_attribution(
        {"from_id": "!legacy", "longname": "Legacy", "shortname": "LG"},
        source_transport_id="!legacy",
    )
    assert fields["source_sender_id"] == "!legacy"
    assert fields["source_sender_label"] == "Legacy"
    assert fields["source_sender_short_label"] == "LG"


# ===================================================================
# Group 7: Platform detection — bare channel excluded, namespaced wins
# ===================================================================


def test_bare_channel_alone_not_detected_as_meshtastic() -> None:
    """Bare ``channel`` alone is too generic to identify Meshtastic native
    data; detection returns None (preserved behaviour)."""
    assert detect_source_platform("generic", {"channel": 0}) is None


def test_bare_channel_with_platform_hint_uses_hint() -> None:
    """platform_hint wins even when native data carries only bare channel."""
    assert (
        detect_source_platform("generic", {"channel": 0}, platform_hint="meshtastic")
        == "meshtastic"
    )


def test_namespaced_channel_alone_detected_as_meshtastic() -> None:
    """A sparse dict carrying only ``meshtastic.channel`` is unambiguously
    Meshtastic-native and is detected as such (namespaced keys are
    unambiguous detection signals, unlike bare ``channel``)."""
    assert detect_source_platform("generic", {"meshtastic.channel": 0}) == "meshtastic"


def test_namespaced_packet_id_alone_detected_as_meshtastic() -> None:
    """A sparse dict carrying only ``meshtastic.packet_id`` is detected as
    Meshtastic-native."""
    assert (
        detect_source_platform("generic", {"meshtastic.packet_id": 42}) == "meshtastic"
    )


def test_namespaced_is_direct_message_alone_detected_as_meshtastic() -> None:
    """A sparse dict carrying only ``meshtastic.is_direct_message`` is
    detected as Meshtastic-native."""
    assert (
        detect_source_platform("generic", {"meshtastic.is_direct_message": True})
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


async def test_matrix_renderer_prefers_namespaced_packet_id() -> None:
    """When both namespaced and bare packet_id are present, the renderer
    emits the namespaced value as mmrelay KEY_ID (meshtastic_id)."""
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={
            "meshtastic.packet_id": "111",
            "packet_id": "999",  # legacy bare; must lose to namespaced
            "longname": "Alice",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == "111"


async def test_matrix_renderer_falls_back_to_bare_packet_id() -> None:
    """When only the bare packet_id is present (legacy stored event), the
    renderer falls back to it for mmrelay KEY_ID."""
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={
            "packet_id": "legacy-77",
            "longname": "Alice",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == "legacy-77"


async def test_matrix_renderer_packet_id_zero_preserved() -> None:
    """A namespaced packet_id of 0 is preserved (first-non-None wins, not
    falsy-or fallback) so it does not erroneously fall through to bare."""
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={
            "meshtastic.packet_id": 0,
            "packet_id": "should-not-win",
            "longname": "Alice",
        },
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == "0"


async def test_matrix_renderer_no_packet_id_emits_empty() -> None:
    """When neither namespaced nor bare packet_id is present, KEY_ID is
    an empty string (no crash, no leakage of other values)."""
    renderer = _make_mmrelay_renderer()
    event = make_meshtastic_event(
        source_adapter="radio-alpha",
        native_data={"longname": "Alice"},
    )
    result = await renderer.render(
        event,
        RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
    )
    assert result.payload["meshtastic_id"] == ""
