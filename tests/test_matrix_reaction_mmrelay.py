"""Focused tests for Matrix codec/renderer/adapter reaction support and
MMRelay metadata edge handling.

Tests cover:
- codec: true m.annotation reaction → MESSAGE_REACTED
- codec: MMRelay emote reaction detection
- codec: MMRelay metadata capture in native data
- renderer: true m.reaction output with _matrix_event_type
- renderer: mmrelay_compat emote fallback
- renderer: reply with KEY_REPLY_ID injection
- adapter: _matrix_event_type popping, default m.room.message
- mmrelay: KEY_REPLY_ID/KEY_EMOJI/EMOJI_FLAG_VALUE constants
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.renderer import RenderingResult
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REPLY_ID,
    KEY_TEXT,
    PORTNUM_TEXT,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_reaction_event(
    emoji: str = "👍",
    target_event_id: str = "$target-001",
    sender: str = "@alice:example.com",
    event_id: str = "$react-001",
    room_id: str = "!room:server",
) -> Any:
    content = {
        "msgtype": "m.text",
        "body": emoji,
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": target_event_id,
            "key": emoji,
        },
    }
    return _make_native_event(
        body=emoji,
        sender=sender,
        event_id=event_id,
        content=content,
    )


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


def _make_canonical_reaction(
    key: str = "👍",
    target_event_id: str = "$target-001",
    adapter_id: str = "matrix-1",
    room_id: str = "!room:server",
    body: str = "👍",
) -> CanonicalEvent:
    """Build a canonical event with a reaction relation for renderer tests."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=NativeRef(
            adapter=adapter_id,
            native_channel_id=room_id,
            native_message_id=target_event_id,
        ),
        key=key,
        fallback_text=None,
    )
    return CanonicalEvent(
        event_id="evt-reaction-001",
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


def _make_canonical_reply(
    body: str = "a reply",
    target_event_id: str = "$orig-001",
    adapter_id: str = "matrix-1",
    room_id: str = "!room:server",
    mmrelay_reply_id: str | None = None,
) -> CanonicalEvent:
    """Build a canonical event with a reply relation."""
    rel = EventRelation(
        relation_type="reply",
        target_event_id=None,
        target_native_ref=NativeRef(
            adapter=adapter_id,
            native_channel_id=room_id,
            native_message_id=target_event_id,
        ),
        key=None,
        fallback_text="original text",
    )
    native_data: dict[str, object] = {"room_id": room_id}
    if mmrelay_reply_id:
        native_data[KEY_REPLY_ID] = mmrelay_reply_id
    return CanonicalEvent(
        event_id="evt-reply-001",
        event_kind=EventKind.MESSAGE_CREATED,
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
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


# ===========================================================================
# Codec tests
# ===========================================================================


class TestCodecTrueReaction:
    """MatrixCodec decodes true m.annotation reactions."""

    def test_true_reaction_is_message_reacted(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_reaction_event(emoji="❤️", target_event_id="$msg-99")
        event = codec.decode(native, room_id="!room:server")

        assert event.event_kind == EventKind.MESSAGE_REACTED

    def test_true_reaction_payload_has_key_and_body(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_reaction_event(emoji="❤️", target_event_id="$msg-99")
        event = codec.decode(native, room_id="!room:server")

        assert event.payload["key"] == "❤️"
        assert event.payload["body"] == "❤️"

    def test_true_reaction_creates_reaction_relation(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_reaction_event(emoji="👍", target_event_id="$msg-42")
        event = codec.decode(native, room_id="!room:server")

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reaction"
        assert rel.key == "👍"
        assert rel.target_native_ref is not None
        assert rel.target_native_ref.native_message_id == "$msg-42"
        assert rel.target_native_ref.adapter == "matrix-1"

    def test_true_reaction_source_native_ref(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_reaction_event(event_id="$react-99")
        event = codec.decode(native, room_id="!room:server")

        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "$react-99"


class TestCodecMMRelayMetadata:
    """MatrixCodec captures MMRelay fields into native metadata."""

    def test_regular_message_captures_mmrelay_fields(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "hello from mesh",
            KEY_ID: "packet-42",
            KEY_MESHNET: "mynetwork",
            KEY_PORTNUM: PORTNUM_TEXT,
            KEY_TEXT: "hello from mesh",
            KEY_REPLY_ID: "node-reply-1",
            KEY_EMOJI: 0,
        }
        native = _make_native_event(body="hello from mesh", content=content)
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert data[KEY_ID] == "packet-42"
        assert data[KEY_MESHNET] == "mynetwork"
        assert data[KEY_PORTNUM] == PORTNUM_TEXT
        assert data[KEY_TEXT] == "hello from mesh"
        assert data[KEY_REPLY_ID] == "node-reply-1"
        assert data[KEY_EMOJI] == 0

    def test_reaction_captures_mmrelay_fields(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.text",
            "body": "👍",
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$msg-1",
                "key": "👍",
            },
            KEY_ID: "pkt-99",
            KEY_MESHNET: "meshnet",
        }
        native = _make_native_event(body="👍", content=content)
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert data[KEY_ID] == "pkt-99"
        assert data[KEY_MESHNET] == "meshnet"

    def test_missing_mmrelay_fields_not_in_native_data(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_native_event(body="plain message")
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert KEY_ID not in data
        assert KEY_MESHNET not in data


class TestCodecMMRelayEmoteReaction:
    """MatrixCodec detects MMRelay-style emote reactions."""

    def test_emote_reaction_is_message_reacted(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(
            body="reacted", reply_id="!abc123", emoji=1
        )
        event = codec.decode(native, room_id="!room:server")

        assert event.event_kind == EventKind.MESSAGE_REACTED

    def test_emote_reaction_creates_reaction_relation(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reaction"
        # key is the body text (the reaction content)
        assert rel.key == "reacted"
        # No native ref — we don't fabricate Meshtastic adapter id
        assert rel.target_native_ref is None
        assert rel.target_event_id is None

    def test_emote_reaction_metadata_has_mmrelay_fields(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert data["meshtastic_reply_id"] == "!abc123"
        assert data["meshtastic_emoji"] == 1

    def test_emote_reaction_relation_metadata(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        rel = event.relations[0]
        assert rel.metadata["meshtastic_reply_id"] == "!abc123"
        assert rel.metadata["meshtastic_emoji"] == 1

    def test_emote_with_emoji_not_1_is_regular_message(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(
            body="just an emote", reply_id="!abc123", emoji=2
        )
        event = codec.decode(native, room_id="!room:server")

        assert event.event_kind == EventKind.MESSAGE_CREATED

    def test_emote_without_replyid_is_regular_message(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        content = {
            "msgtype": "m.emote",
            "body": "waves",
            KEY_EMOJI: 1,
        }
        native = _make_native_event(body="waves", content=content)
        event = codec.decode(native, room_id="!room:server")

        assert event.event_kind == EventKind.MESSAGE_CREATED

    def test_mmrelay_emote_captures_full_mmrelay_fields(self) -> None:
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="!abc123")
        event = codec.decode(native, room_id="!room:server")

        data = event.metadata.native.data
        assert data[KEY_ID] == "packet-42"
        assert data[KEY_MESHNET] == "testnet"
        assert data[KEY_PORTNUM] == PORTNUM_TEXT
        assert data[KEY_TEXT] == "reacted"

    def test_mmrelay_emote_with_reply_id_zero_decodes_to_reaction(self) -> None:
        """MMRelay emote with meshtastic_replyId=0 decodes to reaction relation."""
        codec = MatrixCodec("matrix-1", _make_config())
        native = _make_mmrelay_emote_reaction(body="reacted", reply_id="0")
        event = codec.decode(native, room_id="!room:server")

        assert event.event_kind == EventKind.MESSAGE_REACTED
        assert len(event.relations) == 1
        rel = event.relations[0]
        assert rel.relation_type == "reaction"
        # Metadata should preserve meshtastic_reply_id="0"
        meta = rel.metadata
        assert meta.get("meshtastic_reply_id") == "0"
        assert meta.get("meshtastic_emoji") == 1


# ===========================================================================
# Renderer tests
# ===========================================================================


class TestRendererTrueReaction:
    """MatrixRenderer renders true m.reaction with _matrix_event_type."""

    @pytest.mark.asyncio
    async def test_true_reaction_has_matrix_event_type(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(event, "matrix-1")
        assert result.payload["_matrix_event_type"] == "m.reaction"

    @pytest.mark.asyncio
    async def test_true_reaction_has_annotation_relates_to(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="❤️", target_event_id="$msg-2")
        result = await renderer.render(event, "matrix-1")

        relates = result.payload["m.relates_to"]
        assert relates["rel_type"] == "m.annotation"
        assert relates["event_id"] == "$msg-2"
        assert relates["key"] == "❤️"

    @pytest.mark.asyncio
    async def test_true_reaction_has_no_msgtype_or_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="🔥", target_event_id="$msg-3")
        result = await renderer.render(event, "matrix-1")
        assert "_matrix_event_type" in result.payload
        assert result.payload["_matrix_event_type"] == "m.reaction"
        assert "msgtype" not in result.payload
        assert "body" not in result.payload


class TestRendererMMRelayEmoteFallback:
    """MatrixRenderer mmrelay_compat renders m.emote fallback for reactions."""

    @pytest.mark.asyncio
    async def test_mmrelay_compat_reaction_is_emote(self) -> None:
        renderer = MatrixRenderer(mmrelay_compat=True)
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(event, "matrix-1")

        assert result.payload["msgtype"] == "m.emote"

    @pytest.mark.asyncio
    async def test_mmrelay_compat_reaction_has_reply_id(self) -> None:
        renderer = MatrixRenderer(mmrelay_compat=True)
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(event, "matrix-1")

        assert result.payload[KEY_REPLY_ID] == "$msg-1"

    @pytest.mark.asyncio
    async def test_mmrelay_compat_reaction_has_emoji_flag(self) -> None:
        renderer = MatrixRenderer(mmrelay_compat=True)
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(event, "matrix-1")

        assert result.payload[KEY_EMOJI] == EMOJI_FLAG_VALUE

    @pytest.mark.asyncio
    async def test_mmrelay_compat_reaction_has_text(self) -> None:
        renderer = MatrixRenderer(mmrelay_compat=True)
        event = _make_canonical_reaction(
            key="👍", target_event_id="$msg-1", body="thumbs up"
        )
        result = await renderer.render(event, "matrix-1")

        assert result.payload[KEY_TEXT] == "thumbs up"

    @pytest.mark.asyncio
    async def test_mmrelay_compat_no_matrix_event_type(self) -> None:
        renderer = MatrixRenderer(mmrelay_compat=True)
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(event, "matrix-1")

        assert "_matrix_event_type" not in result.payload

    @pytest.mark.asyncio
    async def test_reply_id_zero_emits_key_reply_id(self) -> None:
        """Reaction with metadata meshtastic_reply_id=0 emits KEY_REPLY_ID='0'."""
        renderer = MatrixRenderer()
        from datetime import datetime, timezone

        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="👍",
            fallback_text=None,
            metadata={"meshtastic_reply_id": 0},
        )
        event = CanonicalEvent(
            event_id="evt-zero-rlid",
            event_kind=EventKind.MESSAGE_REACTED,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@alice:example.com",
            source_channel_id="!room:server",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "👍"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, "matrix-1")
        # No Matrix-native target → no true m.reaction
        assert "_matrix_event_type" not in result.payload
        # But KEY_REPLY_ID must be "0" (preserved, not dropped)
        assert result.payload.get(KEY_REPLY_ID) == "0"

    @pytest.mark.asyncio
    async def test_no_target_falls_back_to_emote(self) -> None:
        """When target is missing, even without mmrelay_compat, use fallback."""
        renderer = MatrixRenderer(mmrelay_compat=False)
        event = _make_canonical_reaction_no_target(key="👍", body="👍")
        result = await renderer.render(event, "matrix-1")

        assert result.payload["msgtype"] == "m.emote"
        assert KEY_EMOJI in result.payload
        assert (
            KEY_REPLY_ID not in result.payload
        )  # no target or metadata to populate it
        assert "_matrix_event_type" not in result.payload


class TestRendererReplyWithReplyId:
    """MatrixRenderer replies inject KEY_REPLY_ID from metadata."""

    @pytest.mark.asyncio
    async def test_reply_injects_reply_id_from_native(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reply(
            body="reply text",
            target_event_id="$orig-1",
            mmrelay_reply_id="node-reply-42",
        )
        result = await renderer.render(event, "matrix-1")

        assert result.payload[KEY_REPLY_ID] == "node-reply-42"

    @pytest.mark.asyncio
    async def test_reply_without_mmrelay_reply_id_uses_target(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reply(
            body="reply text",
            target_event_id="$orig-1",
            mmrelay_reply_id=None,
        )
        result = await renderer.render(event, "matrix-1")

        assert result.payload[KEY_REPLY_ID] == "$orig-1"

    @pytest.mark.asyncio
    async def test_reply_has_in_reply_to(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reply(
            body="reply text",
            target_event_id="$orig-1",
        )
        result = await renderer.render(event, "matrix-1")

        relates = result.payload["m.relates_to"]
        assert relates["m.in_reply_to"]["event_id"] == "$orig-1"


# ===========================================================================
# Adapter tests
# ===========================================================================


class TestMatrixAdapterEventType:
    """MatrixAdapter.deliver correctly handles _matrix_event_type."""

    async def _make_adapter(self) -> MatrixAdapter:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        adapter = MatrixAdapter(config)
        return adapter

    @pytest.mark.asyncio
    async def test_default_message_type_is_m_room_message(self) -> None:
        adapter = await self._make_adapter()
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=type("Resp", (), {"event_id": "$evt-1"})()
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        await adapter.deliver(result)

        actual_message_type = mock_client.room_send.call_args.kwargs.get("message_type")
        actual_content = mock_client.room_send.call_args.kwargs.get("content", {})
        assert (
            actual_message_type == "m.room.message"
        ), f"expected m.room.message, got {actual_message_type}"
        assert "_matrix_event_type" not in actual_content

    @pytest.mark.asyncio
    async def test_reaction_event_type_popped_from_content(self) -> None:
        adapter = await self._make_adapter()
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=type("Resp", (), {"event_id": "$evt-2"})()
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-2",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={
                "_matrix_event_type": "m.reaction",
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target",
                    "key": "👍",
                },
            },
        )
        await adapter.deliver(result)

        actual_message_type = mock_client.room_send.call_args.kwargs.get("message_type")
        actual_content = mock_client.room_send.call_args.kwargs.get("content", {})
        assert (
            actual_message_type == "m.reaction"
        ), f"expected m.reaction, got {actual_message_type}"
        assert "_matrix_event_type" not in actual_content
        assert actual_content.get("m.relates_to", {}).get("key") == "👍"

    def test_adapter_reactions_capability_is_native(self) -> None:
        from medre.adapters.matrix.adapter import _MATRIX_CAPABILITIES

        assert _MATRIX_CAPABILITIES.reactions == "native"


# ===========================================================================
# mmrelay constants tests
# ===========================================================================


class TestMMRelayConstants:
    """Verify mmrelay constants are correct."""

    def test_key_reply_id(self) -> None:
        assert KEY_REPLY_ID == "meshtastic_replyId"

    def test_key_emoji(self) -> None:
        assert KEY_EMOJI == "meshtastic_emoji"

    def test_emoji_flag_value(self) -> None:
        assert EMOJI_FLAG_VALUE == 1

    def test_key_id(self) -> None:
        assert KEY_ID == "meshtastic_id"

    def test_key_text(self) -> None:
        assert KEY_TEXT == "meshtastic_text"

    def test_key_meshnet(self) -> None:
        assert KEY_MESHNET == "meshtastic_meshnet"

    def test_key_portnum(self) -> None:
        assert KEY_PORTNUM == "meshtastic_portnum"

    def test_portnum_text_value(self) -> None:
        assert PORTNUM_TEXT == "TEXT_MESSAGE_APP"
