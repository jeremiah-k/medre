"""Tests for KEY_REACTION_KEY round-tripping and Meshtastic->Matrix mapped reactions.

Split from test_matrix_reaction_mmrelay.py for line-cap compliance.
Tests cover:
- Meshtastic->Matrix reaction rendering for mapped Matrix-originated messages
- Unknown replyId fallback (no crash)
- Renderer emote fallback emits KEY_REACTION_KEY
- Codec decodes KEY_REACTION_KEY into rel.key
- Rendering without KEY_REACTION_KEY
- KEY_REACTION_KEY constant verification
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingContext
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REACTION_KEY,
    KEY_REPLY_ID,
    KEY_TEXT,
    PORTNUM_TEXT,
)
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.matrix_stubs import StubMeshtasticConfig as _StubMeshtasticConfig

# ---------------------------------------------------------------------------
# Helpers (imported from tests.helpers.matrix_stubs)
# ---------------------------------------------------------------------------


# Source-config mappings for common test patterns.
_SRC_MESHTASTIC = {
    "mesh-1": _StubMeshtasticConfig(adapter_id="mesh-1", mmrelay_compatibility=True)
}


def _make_config(**overrides: Any) -> MatrixConfig:
    defaults = dict(
        adapter_id="matrix-1",
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        access_token="tok",
    )
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_native_event(
    body: str = "hello",
    sender: str = "@alice:example.com",
    event_id: str = "$evt-001",
    content: dict | None = None,
    has_source: bool = True,
) -> Any:
    class _Fake:
        pass

    evt = _Fake()
    evt.body = body
    evt.sender = sender
    evt.event_id = event_id
    if has_source:
        evt.source = {
            "content": content or {"msgtype": "m.text", "body": body},
            "event_id": event_id,
            "sender": sender,
            "type": "m.room.message",
        }
    return evt


def _make_mmrelay_emote_reaction(
    body: str = "reacted",
    reply_id: str = "!abc123",
    emoji: int = 1,
    sender: str = "@alice:example.com",
    event_id: str = "$emote-react-001",
) -> Any:
    content = {
        "msgtype": "m.emote",
        "body": body,
        KEY_REPLY_ID: reply_id,
        KEY_EMOJI: emoji,
        KEY_ID: "packet-42",
        KEY_MESHNET: "testnet",
        KEY_PORTNUM: PORTNUM_TEXT,
        KEY_TEXT: body,
    }
    return _make_native_event(
        body=body,
        sender=sender,
        event_id=event_id,
        content=content,
    )


def _make_canonical_reaction_no_target(
    key: str = "👍",
    body: str = "👍",
    adapter_id: str = "matrix-1",
    room_id: str = "!room:server",
) -> CanonicalEvent:
    """Build a canonical reaction with no target native ref."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=None,
        key=key,
        fallback_text=None,
    )
    return CanonicalEvent(
        event_id="evt-reaction-no-target",
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        source_adapter=adapter_id,
        source_transport_id="@alice:example.com",
        source_channel_id=room_id,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body, "msgtype": "m.text"},
        metadata=EventMetadata(native=NativeMetadata(data={"room_id": room_id})),
    )


def _make_mesh_reaction(
    key: str = "👍",
    body: str = "👍",
    fallback_text: str | None = None,
    rel_metadata: dict | None = None,
    native_data: dict | None = None,
    longname: str = "TestNode",
    shortname: str = "TN",
    packet_id: str = "pkt-42",
    source_adapter: str = "mesh-1",
) -> CanonicalEvent:
    """Build a canonical reaction originating from Meshtastic (no Matrix target ref)."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
        metadata=rel_metadata or {},
    )
    nd = (
        native_data
        if native_data is not None
        else {
            "longname": longname,
            "shortname": shortname,
            "packet_id": packet_id,
            "from_id": "!abcdef01",
        }
    )
    return CanonicalEvent(
        event_id="evt-mesh-reaction-001",
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=__import__("datetime").datetime.now(
            tz=__import__("datetime").timezone.utc
        ),
        source_adapter=source_adapter,
        source_transport_id="!node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": body, "msgtype": "m.text"},
        metadata=EventMetadata(native=NativeMetadata(data=nd)),
    )


def _make_mmrelay_emote_reaction_with_key(
    body: str = "reacted",
    reply_id: str = "!abc123",
    emoji: int = 1,
    reaction_key: str = "👍",
    sender: str = "@alice:example.com",
    event_id: str = "$emote-react-rk-001",
) -> Any:
    """Build an MMRelay emote reaction that includes KEY_REACTION_KEY."""
    content = {
        "msgtype": "m.emote",
        "body": body,
        KEY_REPLY_ID: reply_id,
        KEY_EMOJI: emoji,
        KEY_ID: "packet-42",
        KEY_MESHNET: "testnet",
        KEY_PORTNUM: PORTNUM_TEXT,
        KEY_TEXT: body,
        KEY_REACTION_KEY: reaction_key,
    }
    return _make_native_event(
        body=body,
        sender=sender,
        event_id=event_id,
        content=content,
    )


# ===========================================================================
# Meshtastic->Matrix mapped reaction tests
# ===========================================================================


class TestMeshtasticToMatrixMappedReaction:
    """Test C: Meshtastic→Matrix reaction to a mapped Matrix-originated message.

    When a Meshtastic node reacts to a message that was originally from Matrix
    (bridged to Meshtastic), the renderer must produce an MMRelay-style emote
    with the full reaction body format, preserving longname spaces/casing.
    """

    @pytest.mark.asyncio
    async def test_comprehensive_emote_reaction_fields(self) -> None:
        """Single test verifying all required reaction fields together."""
        renderer = MatrixRenderer(
            source_configs={
                "mesh-1": _StubMeshtasticConfig(
                    adapter_id="mesh-1",
                    mmrelay_compatibility=True,
                    meshnet_name="testnet",
                ),
            },
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        original_text = "Hello from mesh world this is a longer test message"
        event = _make_mesh_reaction(
            key="👍",
            body="👍",
            longname="Alpha Bravo",
            shortname="AB",
            packet_id="pkt-77",
            fallback_text=original_text,
            rel_metadata={"meshtastic_reply_id": "2728143522"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        payload = result.payload
        # msgtype must be m.emote, not m.text
        assert payload["msgtype"] == "m.emote"

        # Body must contain the MMRelay reaction format
        body = payload["body"]
        assert 'reacted 👍 to "' in body

        # Body preserves generic Meshtastic longname with spaces and casing
        assert "[Alpha Bravo]" in body

        # Body contains abbreviated original text (40 chars + ...)
        assert "Hello from mesh world this is a longer test message" not in body
        assert "Hello from mesh world this is a longer t..." in body

        # meshtastic_emoji == 1
        assert payload[KEY_EMOJI] == 1

        # meshtastic_replyId == '2728143522'
        assert payload[KEY_REPLY_ID] == "2728143522"

        # NOT rendered as a regular message with only the emoji
        assert body != "👍"
        assert payload["msgtype"] != "m.text"

    @pytest.mark.asyncio
    async def test_mapped_matrix_target_with_compat_emote(self) -> None:
        """Meshtastic reaction targeting a Matrix-originated message via
        target_native_ref still renders as m.emote when mmrelay_compat=True."""
        renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
        # Build a reaction with a Matrix target native ref
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$matrix-orig-001",
            ),
            key="👍",
            fallback_text="original text from Matrix",
            metadata={"meshtastic_reply_id": "2728143522"},
        )
        event = CanonicalEvent(
            event_id="evt-mesh-react",
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            source_adapter="mesh-1",
            source_transport_id="!node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍", "msgtype": "m.text"},
            metadata=EventMetadata(
                native=NativeMetadata(
                    data={
                        "longname": "Alpha Bravo",
                        "shortname": "AB",
                        "packet_id": "pkt-77",
                        "from_id": "!node-1",
                    }
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        # Even with a Matrix target ref, mmrelay_compat=True → emote
        assert result.payload["msgtype"] == "m.emote"
        assert 'reacted 👍 to "original text from Matrix"' in result.payload["body"]
        assert result.payload[KEY_REPLY_ID] == "2728143522"
        # NOT a true m.reaction
        assert "_matrix_event_type" not in result.payload


class TestMeshtasticToMatrixUnknownReplyIdFallback:
    """Missing mapping fallback: Meshtastic→Matrix reaction with unknown replyId.

    Renders MMRelay-style emote when enough metadata exists; safe fallback
    when not. No crash.
    """

    @pytest.mark.asyncio
    async def test_unknown_reply_id_safe_emote(self) -> None:
        """Reaction with no replyId metadata still renders safely as emote."""
        renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
        event = _make_mesh_reaction(
            key="👍",
            body="👍",
            native_data={
                "longname": "Some Node",
                "shortname": "SN",
                "packet_id": "pkt-1",
                "from_id": "!node1",
            },
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert result.payload["msgtype"] == "m.emote"
        assert KEY_EMOJI in result.payload
        assert KEY_REPLY_ID not in result.payload
        assert "reacted" in result.payload["body"]

    @pytest.mark.asyncio
    async def test_minimal_metadata_no_crash(self) -> None:
        """Reaction with completely empty metadata renders without crash."""
        renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
        event = _make_mesh_reaction(
            key="🔥",
            body="🔥",
            native_data={},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert result.payload["msgtype"] == "m.emote"
        assert KEY_EMOJI in result.payload
        assert "reacted 🔥" in result.payload["body"]


# ===========================================================================
# KEY_REACTION_KEY round-trip tests
# ===========================================================================


class TestRendererEmitsReactionKey:
    """Renderer emote fallback emits both KEY_EMOJI == 1 and KEY_REACTION_KEY."""

    @pytest.mark.asyncio
    async def test_emote_fallback_emits_key_reaction_key(self) -> None:
        renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
        event = _make_mesh_reaction(key="👍", body="👍", fallback_text="hello")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert result.payload[KEY_EMOJI] == EMOJI_FLAG_VALUE
        assert result.payload[KEY_REACTION_KEY] == "👍"

    @pytest.mark.asyncio
    async def test_emote_fallback_emits_symbol_not_body(self) -> None:
        """KEY_REACTION_KEY carries the symbol, not the emote body text."""
        renderer = MatrixRenderer(source_configs=_SRC_MESHTASTIC)
        event = _make_mesh_reaction(key="❤️", body="❤️", fallback_text="a message")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert result.payload[KEY_REACTION_KEY] == "❤️"
        # body is the full emote text, not just the emoji
        assert result.payload["body"] != "❤️"
        assert "reacted" in result.payload["body"]

    @pytest.mark.asyncio
    async def test_no_target_also_emits_key_reaction_key(self) -> None:
        """No-target emote fallback also emits KEY_REACTION_KEY."""
        renderer = MatrixRenderer()
        event = _make_canonical_reaction_no_target(key="🔥", body="🔥")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert result.payload[KEY_EMOJI] == EMOJI_FLAG_VALUE
        assert result.payload[KEY_REACTION_KEY] == "🔥"


class TestCodecDecodesReactionKey:
    """Codec decodes MMRelay emote with KEY_REACTION_KEY into rel.key."""

    def test_reaction_key_used_as_rel_key(self) -> None:
        """When KEY_REACTION_KEY is present, codec uses it as rel.key."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="reacted", reply_id="!abc123", reaction_key="👍"
        )
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert rel.key == "👍"

    def test_reaction_key_propagated_to_payload(self) -> None:
        """When KEY_REACTION_KEY differs from body, codec sets payload['key']."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="reacted", reply_id="!abc123", reaction_key="❤️"
        )
        event = codec.decode(native, room_id="!room:server")

        assert event.payload["key"] == "❤️"

    def test_reaction_key_in_relation_metadata(self) -> None:
        """When KEY_REACTION_KEY is present, it appears in rel.metadata."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="reacted", reply_id="!abc123", reaction_key="🔥"
        )
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert rel.metadata.get("meshtastic_reaction_key") == "🔥"

    def test_reaction_key_in_native_data(self) -> None:
        """KEY_REACTION_KEY is captured into native_data via _capture_mmrelay_fields."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="reacted", reply_id="!abc123", reaction_key="👍"
        )
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert data.get(KEY_REACTION_KEY) == "👍"

    def test_reaction_key_equals_body_still_in_payload(self) -> None:
        """When KEY_REACTION_KEY == body, payload['key'] is still set."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="👍", reply_id="!abc123", reaction_key="👍"
        )
        event = codec.decode(native, room_id="!room:server")

        assert event.payload["key"] == "👍"
        assert event.payload["body"] == "👍"

    def test_reaction_key_equals_body_still_in_metadata(self) -> None:
        """When KEY_REACTION_KEY == body, rel.metadata still has the key."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction_with_key(
            body="👍", reply_id="!abc123", reaction_key="👍"
        )
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert rel.metadata.get("meshtastic_reaction_key") == "👍"


class TestCodecBackwardCompatNoReactionKey:
    """Codec still decodes old MMRelay emotes without KEY_REACTION_KEY."""

    def test_no_reaction_key_falls_back_to_body(self) -> None:
        """Without KEY_REACTION_KEY, codec falls back to body for rel.key."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert rel.key == "reacted"

    def test_no_reaction_key_no_payload_key(self) -> None:
        """Without KEY_REACTION_KEY, payload has no 'key' field."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        assert "key" not in event.payload

    def test_no_reaction_key_no_metadata_key(self) -> None:
        """Without KEY_REACTION_KEY, rel.metadata has no meshtastic_reaction_key."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert "meshtastic_reaction_key" not in rel.metadata


class TestReactionKeyConstant:
    """Verify KEY_REACTION_KEY constant."""

    def test_key_reaction_key_value(self) -> None:
        assert KEY_REACTION_KEY == "meshtastic_reaction_key"
