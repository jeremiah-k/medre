# medre-test-tier: compatibility
"""Focused tests for Matrix codec/renderer/adapter reaction support.

Tests cover:
- codec: true m.annotation reaction → MESSAGE_REACTED
- codec: flat MMRelay content hints do not override the body or invent reactions
- renderer: true m.reaction emitted as a send_event operation
- renderer: emote fallback for reactions without a Matrix-native target,
  with the attribution prefix and quoted original-text preview
- renderer: original-text preview extraction from canonical relation
  enrichment (relation metadata, then fallback_text)
- adapter: closed _matrix_operation envelope dispatch, event_type wiring
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.outbound import MatrixOutboundOperation
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.events.canonical import CanonicalEvent, EventRelation, NativeRef
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata
from medre.core.rendering.relations import reaction_target_preview
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from tests.helpers.matrix import matrix_payload_content as _payload_content
from tests.helpers.matrix_adapter import wire_mock_session as _wire_mock_session
from tests.helpers.matrix_stubs import StubMatrixConfig as _StubMatrixConfig
from tests.helpers.native_metadata import matrix_native_data

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
        metadata=EventMetadata(
            native=NativeMetadata(data=matrix_native_data({"room_id": room_id}))
        ),
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
        metadata=EventMetadata(
            native=NativeMetadata(data=matrix_native_data({"room_id": room_id}))
        ),
    )


def _make_canonical_reply(
    body: str = "a reply",
    target_event_id: str = "$orig-001",
    adapter_id: str = "matrix-1",
    room_id: str = "!room:server",
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


# ===========================================================================
# Renderer tests
# ===========================================================================


class TestRendererDescribedReaction:
    """MatrixRenderer renders reactions as described m.emote room messages."""

    @pytest.mark.asyncio
    async def test_described_reaction_has_matrix_event_type(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="👍", target_event_id="$msg-1")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"

    @pytest.mark.asyncio
    async def test_described_reaction_names_key_in_body(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="❤️", target_event_id="$msg-2")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        content = _payload_content(result)
        assert content["msgtype"] == "m.emote"
        assert "reacted ❤️ to" in content["body"]

    @pytest.mark.asyncio
    async def test_described_reaction_has_no_annotation_relates_to(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reaction(key="🔥", target_event_id="$msg-3")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"
        assert "m.relates_to" not in _payload_content(result)


class TestReactionEmoteFallback:
    """Reactions without a Matrix-native target render the m.emote fallback."""

    @pytest.mark.asyncio
    async def test_no_target_falls_back_to_emote(self) -> None:
        """When target is missing, use fallback."""
        renderer = MatrixRenderer()
        event = _make_canonical_reaction_no_target(key="👍", body="👍")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        assert _payload_content(result)["msgtype"] == "m.emote"
        # Foreign MMRelay wire keys stay absent: no target or metadata exists
        # to populate them.
        assert "meshtastic_emoji" not in _payload_content(result)
        assert "meshtastic_replyId" not in _payload_content(result)
        assert result.payload["_matrix_operation"]["event_type"] == "m.room.message"


class TestRendererReplyRelation:
    """Matrix-native replies reference the target through m.in_reply_to."""

    @pytest.mark.asyncio
    async def test_reply_has_in_reply_to(self) -> None:
        renderer = MatrixRenderer()
        event = _make_canonical_reply(
            body="reply text",
            target_event_id="$orig-1",
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        relates = _payload_content(result)["m.relates_to"]
        assert relates["m.in_reply_to"]["event_id"] == "$orig-1"


# ===========================================================================
# Adapter tests
# ===========================================================================


class TestMatrixAdapterEventType:
    """MatrixAdapter.deliver dispatches on the closed operation envelope."""

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
        _wire_mock_session(adapter, mock_client)

        operation = MatrixOutboundOperation.send_event(
            "m.room.message", {"msgtype": "m.text", "body": "hello"}
        )
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload=operation.to_payload(),
        )
        await adapter.deliver(result)

        actual_message_type = mock_client.room_send.call_args.kwargs.get("message_type")
        actual_content = mock_client.room_send.call_args.kwargs.get("content", {})
        assert (
            actual_message_type == "m.room.message"
        ), f"expected m.room.message, got {actual_message_type}"
        assert "_matrix_operation" not in actual_content

    @pytest.mark.asyncio
    async def test_reaction_event_type_from_envelope(self) -> None:
        adapter = await self._make_adapter()
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=type("Resp", (), {"event_id": "$evt-2"})()
        )
        _wire_mock_session(adapter, mock_client)

        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$target",
                "key": "👍",
            },
        }
        operation = MatrixOutboundOperation.send_event("m.reaction", content)
        result = RenderingResult(
            event_id="evt-2",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload=operation.to_payload(),
        )
        await adapter.deliver(result)

        actual_message_type = mock_client.room_send.call_args.kwargs.get("message_type")
        actual_content = mock_client.room_send.call_args.kwargs.get("content", {})
        assert (
            actual_message_type == "m.reaction"
        ), f"expected m.reaction, got {actual_message_type}"
        assert "_matrix_operation" not in actual_content
        assert actual_content.get("m.relates_to", {}).get("key") == "👍"

    def test_adapter_reactions_capability_is_fallback(self) -> None:
        from medre.adapters.matrix.adapter import _MATRIX_CAPABILITIES

        assert _MATRIX_CAPABILITIES.reactions == "fallback"


# ===========================================================================
# Meshtastic→Matrix reaction rendering (MMRelay emote)
# ===========================================================================


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
    if native_data is not None:
        nd = native_data
    else:
        # The meshtastic adapter persists transport-native identity under
        # the versioned ``native.meshtastic`` namespace; attribution and
        # sender projection read only that namespace.
        nd = {
            "meshtastic": {
                "schema_version": 1,
                "longname": longname,
                "shortname": shortname,
                "packet_id": packet_id,
                "from_id": "!abcdef01",
            },
        }
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


class TestMMRelayReactionBodyFormat:
    """MMRelay emote body format for Meshtastic-originated reactions."""

    @pytest.mark.asyncio
    async def test_emote_body_has_leading_newline_and_prefix(self) -> None:
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_mesh_reaction(key="❤️", body="❤️", fallback_text="hello world")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        body = _payload_content(result)["body"]
        assert body.startswith("\n ")
        assert "[TestNode]" in body
        assert "reacted ❤️" in body
        assert 'to "hello world"' in body

    @pytest.mark.asyncio
    async def test_emote_body_without_prefix(self) -> None:
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(key="👍", fallback_text="original msg")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        body = _payload_content(result)["body"]
        assert body == '\n reacted 👍 to "original msg"'

    @pytest.mark.asyncio
    async def test_emote_body_with_empty_original_text(self) -> None:
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(key="👍", body="👍")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        body = _payload_content(result)["body"]
        # An unresolvable original text renders the honest fallback wording,
        # never an empty quotation.
        assert body == "\n reacted 👍 to an earlier message"

    @pytest.mark.asyncio
    async def test_emote_body_abbreviates_long_text(self) -> None:
        renderer = MatrixRenderer()
        long_text = "A" * 50
        event = _make_mesh_reaction(key="🔥", fallback_text=long_text)
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        body = _payload_content(result)["body"]
        # 40 chars + "..." inside quotes
        assert 'to "' + "A" * 40 + '..."' in body
        assert ("A" * 41 + "...") not in body

    @pytest.mark.asyncio
    async def test_emote_body_normalizes_newlines(self) -> None:
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(key="👍", fallback_text="line1\nline2\r\nline3")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )

        body = _payload_content(result)["body"]
        assert "\n" not in body.split('to "')[1].rstrip('"')
        assert "line1 line2 line3" in body


class TestOriginalTextExtraction:
    """Original text preference: relation metadata → fallback_text → empty."""

    def test_from_relation_metadata_original_text(self) -> None:
        event = _make_mesh_reaction(
            key="👍",
            rel_metadata={"original_text": "original from meta"},
            fallback_text="fallback",
        )
        text = reaction_target_preview(event.relations[0])
        assert text == "original from meta"

    def test_from_fallback_text(self) -> None:
        event = _make_mesh_reaction(key="👍", fallback_text="fallback text")
        text = reaction_target_preview(event.relations[0])
        assert text == "fallback text"

    def test_empty_when_no_sources(self) -> None:
        event = _make_mesh_reaction(key="👍")
        text = reaction_target_preview(event.relations[0])
        assert text == ""


class TestAbbreviateText:
    """Text abbreviation: normalize newlines, truncate at 40 chars."""

    def test_short_text_unchanged(self) -> None:
        assert (
            reaction_target_preview(
                EventRelation("reaction", "target", None, "👍", "hello")
            )
            == "hello"
        )

    def test_exact_40_chars_unchanged(self) -> None:
        text = "A" * 40
        assert (
            reaction_target_preview(
                EventRelation("reaction", "target", None, "👍", text)
            )
            == text
        )

    def test_41_chars_truncated_with_ellipsis(self) -> None:
        text = "A" * 41
        result = reaction_target_preview(
            EventRelation("reaction", "target", None, "👍", text)
        )
        assert result == "A" * 40 + "..."
        assert len(result) == 43

    def test_newlines_normalized_to_spaces(self) -> None:
        assert (
            reaction_target_preview(
                EventRelation("reaction", "target", None, "👍", "line1\nline2")
            )
            == "line1 line2"
        )

    def test_carriage_returns_normalized(self) -> None:
        assert (
            reaction_target_preview(
                EventRelation("reaction", "target", None, "👍", "a\r\nb")
            )
            == "a b"
        )


class TestReactionPrefixPreservesLongname:
    """Prefix preserves Meshtastic longname exactly: spaces, casing, emoji."""

    @pytest.mark.asyncio
    async def test_prefix_preserves_spaces_in_longname(self) -> None:
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_mesh_reaction(longname="  Space Node  ")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert "[  Space Node  ]" in body

    @pytest.mark.asyncio
    async def test_prefix_preserves_casing(self) -> None:
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_mesh_reaction(longname="CamelCaseNode")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert "[CamelCaseNode]" in body

    @pytest.mark.asyncio
    async def test_prefix_preserves_emoji_in_longname(self) -> None:
        renderer = MatrixRenderer(
            configs={
                "matrix-1": _StubMatrixConfig(
                    adapter_id="matrix-1",
                    relay_prefix="[{sender}] ",
                ),
            },
        )
        event = _make_mesh_reaction(longname="🚀RocketNode")
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        body = _payload_content(result)["body"]
        assert "[🚀RocketNode]" in body


class TestReactionNoTargetNoCrash:
    """Degenerate metadata must not crash the emote fallback path."""

    @pytest.mark.asyncio
    async def test_no_target_no_metadata_no_crash(self) -> None:
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(
            key="👍",
            body="👍",
            native_data={},  # no native data at all
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert _payload_content(result)["msgtype"] == "m.emote"
        assert "reacted 👍" in _payload_content(result)["body"]

    @pytest.mark.asyncio
    async def test_missing_fields_use_defaults(self) -> None:
        renderer = MatrixRenderer()
        event = _make_mesh_reaction(native_data={})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # Unresolvable original text renders the honest fallback wording.
        assert "reacted 👍 to an earlier message" in _payload_content(result)["body"]


class TestReplyNoMatrixTargetNoInReplyTo:
    """Unknown replyId: no m.in_reply_to, but still carry meshtastic_replyId."""

    @pytest.mark.asyncio
    async def test_no_matrix_native_target_no_in_reply_to(self) -> None:
        renderer = MatrixRenderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-reply-no-target",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            source_adapter="mesh-1",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "my reply"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        assert "m.relates_to" not in _payload_content(result)

    @pytest.mark.asyncio
    async def test_foreign_native_ref_reply_no_in_reply_to(self) -> None:
        renderer = MatrixRenderer()
        foreign_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="99"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=foreign_ref,
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-foreign-reply",
            event_kind=EventKind.MESSAGE_CREATED,
            schema_version=1,
            timestamp=__import__("datetime").datetime.now(
                tz=__import__("datetime").timezone.utc
            ),
            source_adapter="mesh-1",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "reply text"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="matrix_instance", delivery_strategy="direct"
            ),
        )
        assert "m.relates_to" not in _payload_content(result)
