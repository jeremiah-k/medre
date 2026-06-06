"""Tests for MeshtasticRenderer relation rendering: cross-platform descriptive
reactions, fallback-text delivery strategy, relation degradation, and
targeted coverage paths for relation handling.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
)
from medre.core.rendering.renderer import RenderingContext

# ---------------------------------------------------------------------------
# Helpers (duplicated from test_meshtastic_renderer.py)
# ---------------------------------------------------------------------------


def _make_renderer(
    target_adapter: str = "mesh-1",
    *,
    radio_relay_prefix: str = "",
    meshnet_name: str = "",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    """Create a MeshtasticRenderer with a single-adapter config mapping."""
    config = MeshtasticConfig(
        adapter_id=target_adapter,
        radio_relay_prefix=radio_relay_prefix,
        meshnet_name=meshnet_name,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={target_adapter: config})


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="mesh-1",
        source_transport_id="!node1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "hello mesh"},
        metadata=EventMetadata(),
    )


def _make_relation(
    relation_type: str = "reply",
    native_message_id: str | None = "42",
    key: str | None = None,
    fallback_text: str | None = None,
    adapter_id: str = "mesh-1",
) -> EventRelation:
    native_ref = None
    if native_message_id is not None:
        native_ref = NativeRef(
            adapter=adapter_id,
            native_channel_id="0",
            native_message_id=native_message_id,
        )
    return EventRelation(
        relation_type=relation_type,
        target_event_id="evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
    )


# ===================================================================
# Helper factories for Matrix-originated events
# ===================================================================


def _make_matrix_event(
    event_id: str = "mx-evt-1",
    payload: dict | None = None,
    relations: tuple | None = None,
    source_adapter: str = "matrix-1",
    display_name: str = "Display Name",
) -> CanonicalEvent:
    """Create a CanonicalEvent simulating Matrix origin."""
    native_data: dict[str, object] = {
        "longname": display_name,
        "shortname": display_name.split()[0] if display_name else "",
        "from_id": "@user:example.com",
    }
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.reacted",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="@user:example.com",
        source_channel_id="!room:example.com",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"body": "👍"},
        metadata=EventMetadata(native=NativeMetadata(data=native_data)),
    )


def _make_cross_platform_relation(
    key: str = "👍",
    fallback_text: str | None = "original mesh message",
    meshtastic_reply_id: str | None = None,
    mesh_adapter: str = "mesh-1",
) -> EventRelation:
    """Create a reaction relation pointing at a Meshtastic message.

    If *meshtastic_reply_id* is given, sets both the target_native_ref
    (owned by *mesh_adapter*) and the mmrelay metadata fallback.
    """
    metadata: dict[str, object] = {}
    native_ref = None
    if meshtastic_reply_id is not None:
        native_ref = NativeRef(
            adapter=mesh_adapter,
            native_channel_id="0",
            native_message_id=meshtastic_reply_id,
        )
        metadata["meshtastic_reply_id"] = meshtastic_reply_id
    return EventRelation(
        relation_type="reaction",
        target_event_id="mesh-evt-0",
        target_native_ref=native_ref,
        key=key,
        fallback_text=fallback_text,
        metadata=metadata,
    )


# ===================================================================
# Cross-platform (Matrix→Meshtastic) MMRelay descriptive reactions
# ===================================================================


class TestCrossPlatformReactionDescriptive:
    """Matrix-originated reactions render as MMRelay descriptive text."""

    async def test_descriptive_text_with_reply_id(self) -> None:
        """Matrix reaction with Meshtastic mapping → descriptive text + reply_id."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="hello from mesh",
            meshtastic_reply_id="42",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )

        # reply_id is set (mapped Meshtastic packet ID)
        assert result.payload["reply_id"] == 42
        # NO emoji=1 — descriptive, not native tapback
        assert "emoji" not in result.payload
        # Descriptive text pattern
        text = result.payload["text"]
        assert "reacted 👍 to" in text
        assert "hello from mesh" in text

    async def test_descriptive_text_without_reply_id(self) -> None:
        """Matrix reaction without Meshtastic mapping → descriptive text only."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="❤️",
            fallback_text="some original",
            meshtastic_reply_id=None,
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )

        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        text = result.payload["text"]
        assert 'reacted ❤️ to "some original"' in text

    async def test_no_emoji_field_set(self) -> None:
        """Cross-platform reactions never set emoji=1."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="🔥",
            fallback_text="msg",
            meshtastic_reply_id="99",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload.get("emoji") is None

    async def test_compact_prefix_strips_spaces_preserves_casing(self) -> None:
        """Display name spaces are stripped in the prefix; casing preserved."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
            meshnet_name="testnet",
        )

        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test msg",
            meshtastic_reply_id="10",
        )
        # "Display Name" → "DisplayName" in prefix
        event = _make_matrix_event(
            display_name="Display Name",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "[DisplayName] reacted" in text
        # NOT lowercased
        assert "[displayname]" not in text

    async def test_compact_prefix_not_lowercased(self) -> None:
        """Casing is preserved: 'MeshUser' stays 'MeshUser', not 'meshuser'."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{longname}] ",
        )

        rel = _make_cross_platform_relation(key="👋", fallback_text="hi")
        event = _make_matrix_event(
            display_name="Mesh User",
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "[MeshUser] reacted" in text

    async def test_abbreviated_preview_40_chars(self) -> None:
        """Original text preview is abbreviated to 40 chars + '...'."""
        renderer = _make_renderer("mesh-1")
        long_text = "A" * 60
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text=long_text,
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        # Should contain abbreviated text (40 chars + "...")
        assert "A" * 40 + '..."' in text
        # Should NOT contain the full 60 chars
        assert "A" * 60 not in text

    async def test_abbreviated_preview_short_text_unchanged(self) -> None:
        """Short original text is not truncated."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="short msg",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert 'reacted 👍 to "short msg"' in text

    async def test_newlines_normalised_to_spaces(self) -> None:
        """Newlines in original text are replaced with spaces."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="line one\nline two\nline three",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "\n" not in text.split('to "')[1]
        assert "line one line two line three" in text

    async def test_quoted_reply_lines_stripped(self) -> None:
        """Quoted reply lines (> ...) are stripped from preview."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="> quoted line\nactual message",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "> quoted" not in text
        assert "actual message" in text

    async def test_original_text_from_metadata_preferred(self) -> None:
        """relation.metadata['original_text'] takes priority over fallback_text."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="fallback text",
        )
        # Inject original_text into metadata
        meta = dict(rel.metadata)
        meta["original_text"] = "metadata original"
        rel2 = EventRelation(
            relation_type=rel.relation_type,
            target_event_id=rel.target_event_id,
            target_native_ref=rel.target_native_ref,
            key=rel.key,
            fallback_text=rel.fallback_text,
            metadata=meta,
        )
        event = _make_matrix_event(relations=(rel2,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "metadata original" in text
        assert "fallback text" not in text

    async def test_falls_back_to_payload_body(self) -> None:
        """When no fallback_text, uses event payload body/text."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text=None,
        )
        event = _make_matrix_event(
            payload={"body": "payload body text"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        text = result.payload["text"]
        assert "payload body text" in text

    async def test_preserves_channel_and_meshnet(self) -> None:
        """Cross-platform reaction preserves channel_index and meshnet_name."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="😀",
            fallback_text="hi",
            meshtastic_reply_id="7",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="direct", target_channel="4"
            ),
        )
        assert result.payload["channel_index"] == 4
        assert "meshnet_name" in result.payload

    async def test_metadata_includes_descriptive_reaction_flag(self) -> None:
        """Result metadata has descriptive_reaction=True for cross-platform."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.metadata.get("descriptive_reaction") is True

    async def test_no_radio_relay_prefix_in_metadata_for_descriptive(self) -> None:
        """Descriptive reactions embed their own prefix; no separate prefix metadata."""
        renderer = _make_renderer("mesh-1")
        rel = _make_cross_platform_relation(
            key="👍",
            fallback_text="test",
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "radio_relay_prefix" not in result.metadata

    async def test_mmrelay_metadata_reply_id_still_works(self) -> None:
        """Cross-platform reaction with mmrelay metadata gets reply_id."""
        renderer = _make_renderer("mesh-1")
        # No native ref (meshtastic_reply_id=None in helper means no native ref)
        # but we add meshtastic_reply_id via metadata
        rel = EventRelation(
            relation_type="reaction",
            target_event_id=None,
            target_native_ref=None,
            key="👍",
            fallback_text="mesh msg",
            metadata={"meshtastic_reply_id": "88"},
        )
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["reply_id"] == 88
        assert "emoji" not in result.payload


# ===================================================================
# Fallback-text delivery strategy: reply relation context preservation
# ===================================================================


class TestFallbackTextReplyRelationContext:
    """fallback_text delivery strategy must preserve reply relation context
    even when rel.fallback_text is absent.  The marker uses
    target_native_ref.native_message_id or target_event_id as a
    deterministic identifier.  No native reply_id is emitted.
    """

    async def test_reply_with_fallback_text_present(self) -> None:
        """When rel.fallback_text exists, marker uses fallback_text value."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="42",
            fallback_text="original msg",
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert "[replying to: original msg]" in text
        assert "my reply" in text
        # No native reply_id under fallback_text mode
        assert "reply_id" not in result.payload

    async def test_reply_without_fallback_text_with_native_ref(self) -> None:
        """When fallback_text and target_event_id are both absent,
        marker uses native_message_id from target_native_ref."""
        renderer = _make_renderer("mesh-1")
        native_ref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert "[replying to: 42]" in text
        assert "my reply" in text
        # No native reply_id under fallback_text mode
        assert "reply_id" not in result.payload

    async def test_reply_without_fallback_text_with_target_event_id(
        self,
    ) -> None:
        """When fallback_text and target_native_ref are both absent,
        marker uses target_event_id."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-abc123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert "[replying to: evt-abc123]" in text
        assert "my reply" in text
        assert "reply_id" not in result.payload

    async def test_reply_no_target_info_plain_text(self) -> None:
        """When no fallback_text, no native_ref, no target_event_id,
        no marker is prepended — plain body text."""
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert text == "my reply"
        assert "[replying to:" not in text
        assert "reply_id" not in result.payload

    async def test_target_event_id_preferred_over_native_ref(self) -> None:
        """When both target_event_id and target_native_ref exist,
        target_event_id is preferred for the marker."""
        renderer = _make_renderer("mesh-1")
        native_ref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="99",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-override",
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert "[replying to: evt-override]" in text
        assert "99" not in text

    async def test_preserves_channel_index_and_meshnet_name(self) -> None:
        """Fallback-text reply preserves channel_index and meshnet_name."""
        renderer = _make_renderer("mesh-1", meshnet_name="testnet")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="42",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1",
                delivery_strategy="fallback_text",
                target_channel="3",
            ),
        )
        assert result.payload["channel_index"] == 3
        assert result.payload["meshnet_name"] == "testnet"
        assert "reply_id" not in result.payload

    async def test_byte_truncation_preserved(self) -> None:
        """Fallback-text reply respects byte truncation budget."""
        renderer = _make_renderer("mesh-1", max_text_bytes=30)
        native_ref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-0",
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "A" * 200},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        assert len(result.payload["text"].encode("utf-8")) <= 30
        assert result.truncated is True
        assert "reply_id" not in result.payload

    async def test_prefix_applied_to_fallback_reply(self) -> None:
        """Fallback-text reply gets radio_relay_prefix prepended."""
        renderer = _make_renderer(
            "mesh-1",
            radio_relay_prefix="[{shortname5}] ",
        )
        native_ref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-1",
            source_transport_id="@user:example.com",
            source_channel_id="!room:example.com",
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "my reply"},
            metadata=EventMetadata(
                native=NativeMetadata(data={"shortname": "Test", "from_id": "1"})
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        text = result.payload["text"]
        assert text.startswith("[Test] ")
        assert "[replying to: 42]" in text
        assert "reply_id" not in result.payload

    async def test_metadata_includes_delivery_strategy(self) -> None:
        """Fallback-text rendering metadata includes delivery_strategy."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reply",
            native_message_id="42",
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": "my reply"},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        assert result.metadata.get("delivery_strategy") == "fallback_text"
        assert result.fallback_applied == "strategy_fallback_text"


# ===================================================================
# Targeted coverage: reaction emoji body fallback, unknown relation
# catch-all, _resolve_reply_target_marker
# ===================================================================


class TestTargetedCoveragePaths:
    """Pinpoint tests for code paths that previously lacked coverage."""

    async def test_direct_reaction_emoji_falls_back_to_payload_body(
        self,
    ) -> None:
        """When rel.key is None and payload lacks 'key', emoji resolves to
        payload['body'] via the step-by-step resolution order:
        rel.key → payload["key"] → payload["emoji"] → payload["body"].
        """
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="reaction",
            native_message_id="99",
            key=None,
        )
        # payload has no "key" field — should fall back to "body"
        event = _make_event(
            payload={"body": "🔥"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == "🔥"
        assert result.payload["reply_id"] == 99
        assert result.payload["emoji"] == 1

    async def test_direct_unknown_relation_type_delegates_to_extract_text(
        self,
    ) -> None:
        """An unrecognised relation type (e.g. 'thread') hits the else catch-all
        which delegates to _extract_text.  No native reply_id or emoji fields
        are emitted for unknown relation types.
        """
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(
            relation_type="thread",
            native_message_id="10",
        )
        event = _make_event(
            payload={"body": "thread message content"},
            relations=(rel,),
        )
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert result.payload["text"] == "thread message content"
        # No reply_id or emoji fields for unknown relation types
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload

    def test_resolve_reply_target_marker_returns_native_message_id(self) -> None:
        """_resolve_reply_target_marker returns native_message_id when
        target_event_id is absent.

        Exercises renderer.py lines:
            ref = rel.target_native_ref
            if ref is not None:
                mid = getattr(ref, "native_message_id", None)
                if mid is not None:
                    return str(mid)
        """
        native_ref = NativeRef(
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        result = MeshtasticRenderer._resolve_reply_target_marker(rel)
        assert result == "42"


# ===================================================================
# Fallback-text delivery strategy: edit, delete, thread relation types
# ===================================================================


class TestFallbackTextOtherRelationTypes:
    """fallback_text delivery strategy for edit, delete, and thread
    relation types.  These hit the explicit branches in _render_fallback_text.
    """

    @pytest.mark.parametrize(
        ("relation_type", "body", "expected_in_text"),
        [
            ("edit", "updated content", "[edited] updated content"),
            ("delete", "unused", "[deleted: evt-0]"),
            ("thread", "thread reply text", "[thread: evt-0] thread reply text"),
        ],
        ids=["edit", "delete", "thread"],
    )
    async def test_fallback_text_passthrough_for_relation_type(
        self,
        relation_type: str,
        body: str,
        expected_in_text: str,
    ) -> None:
        """edit/delete/thread relations in fallback_text mode pass through
        to _extract_text.  No native reply_id or emoji fields are emitted.
        """
        renderer = _make_renderer("mesh-1")
        rel = EventRelation(
            relation_type=relation_type,
            target_event_id="evt-0",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"body": body},
            relations=(rel,),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        assert expected_in_text in result.payload["text"]
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"


class TestRelationDegradationAndTruncationEvidence:
    """Native reaction fallback degradation and truncation evidence."""

    async def test_native_reaction_fallback_degrades(self) -> None:
        """Same-adapter reaction in fallback_text: [reacted: X], no emoji."""
        renderer = _make_renderer("mesh-1")
        rel = _make_relation(relation_type="reaction", native_message_id="55", key="👍")
        event = _make_event(payload={"body": "👍"}, relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        assert "[reacted: 👍]" in result.payload["text"]
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        assert "descriptive_reaction" not in result.metadata

    async def test_fallback_reply_truncation_preserves_context(self) -> None:
        """Truncated fallback reply still shows [replying to: X] marker."""
        renderer = _make_renderer("mesh-1", max_text_bytes=30)
        native_ref = NativeRef(
            adapter="mesh-1", native_channel_id="0", native_message_id="42"
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=native_ref,
            key=None,
            fallback_text=None,
        )
        event = _make_event(payload={"body": "X" * 200}, relations=(rel,))
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="mesh-1", delivery_strategy="fallback_text"
            ),
        )
        assert "[replying to: 42]" in result.payload["text"]
        assert result.truncated is True
        assert result.metadata["original_text_bytes"] > 30
        assert result.metadata["rendered_text_bytes"] <= 30

    async def test_cross_reaction_truncation_preserves_emoji(self) -> None:
        """Truncated cross-platform reaction preserves 'reacted X' evidence."""
        renderer = _make_renderer("mesh-1", max_text_bytes=25)
        rel = _make_cross_platform_relation(key="❤️", fallback_text="A" * 80)
        event = _make_matrix_event(relations=(rel,))
        result = await renderer.render(
            event, RenderingContext(target_adapter="mesh-1", delivery_strategy="direct")
        )
        assert "reacted ❤️" in result.payload["text"]
        assert result.truncated is True
