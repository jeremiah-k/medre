"""Tests for LxmfRenderer: name, can_render dispatch, rendering output,
title, fields envelope, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingContext, RenderingResult


def _make_event(
    event_id: str = "evt-1",
    payload: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="lxmf-1",
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello lxmf"},
        metadata=EventMetadata(),
    )


def _make_event_with_relations(
    event_id: str = "evt-rel-1",
    payload: dict | None = None,
) -> CanonicalEvent:
    """Create an event with a reply relation for testing fallback behavior."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="lxmf-1",
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=("parent-1",),
        relations=(
            EventRelation(
                relation_type="reply",
                target_event_id="evt-original",
                target_native_ref=NativeRef(
                    adapter="lxmf-1",
                    native_channel_id=None,
                    native_message_id="abc123",
                ),
                key=None,
                fallback_text="original message text",
            ),
        ),
        payload=payload or {"body": "hello reply"},
        metadata=EventMetadata(),
    )


class TestLxmfRenderer:
    """LxmfRenderer output and dispatch tests."""

    def test_name_is_lxmf(self) -> None:
        renderer = LxmfRenderer()
        assert renderer.name == "lxmf"

    def test_can_render_lxmf_platform(self) -> None:
        """Renderer matches when target_platform is lxmf."""
        renderer = LxmfRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="local-rnode",
                    delivery_strategy="direct",
                    target_platform="lxmf",
                ),
            )
            is True
        )

    def test_can_render_non_lxmf(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="fake_presentation",
                    delivery_strategy="direct",
                    target_platform="fake",
                ),
            )
            is False
        )

    def test_can_render_rejects_matrix(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="matrix_instance",
                    delivery_strategy="direct",
                    target_platform="matrix",
                ),
            )
            is False
        )

    def test_can_render_without_platform_returns_false(self) -> None:
        """Without platform info, renderer cannot match (no prefix fallback)."""
        renderer = LxmfRenderer()
        event = _make_event()
        assert (
            renderer.can_render(
                event,
                RenderingContext(
                    target_adapter="lxmf_node", delivery_strategy="direct"
                ),
            )
            is False
        )

    async def test_render_basic_content(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "hello lxmf"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert isinstance(result, RenderingResult)
        assert result.payload["content"] == "hello lxmf"

    async def test_render_with_title(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "body", "title": "Subject"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "body"
        assert result.payload["title"] == "Subject"

    async def test_render_empty_content(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": ""})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == ""

    async def test_render_extracts_body_field(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "specific body"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "body" not in result.payload
        assert result.payload["content"] == "specific body"

    async def test_render_falls_back_to_text_field(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event(payload={"text": "fallback text"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "fallback text"

    async def test_render_payload_has_content_not_text_key(self) -> None:
        """Payload uses 'content' key, not 'text'."""
        renderer = LxmfRenderer()
        event = _make_event(payload={"body": "check keys"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "content" in result.payload
        assert "text" not in result.payload

    async def test_render_includes_destination_hash(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "destination_hash" in result.payload
        assert result.payload["destination_hash"] == ""

    async def test_render_includes_fields(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "fields" in result.payload
        assert isinstance(result.payload["fields"], dict)

    async def test_render_fields_envelope_embedded(self) -> None:
        renderer = LxmfRenderer(metadata_embedding=True)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        fields = result.payload["fields"]
        from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE

        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE]
        assert LXMF_NAMESPACE in envelope
        assert envelope[LXMF_NAMESPACE]["event_id"] == "evt-1"

    async def test_render_no_envelope_when_disabled(self) -> None:
        renderer = LxmfRenderer(metadata_embedding=False)
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        fields = result.payload["fields"]
        from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE

        assert FIELD_MEDRE_ENVELOPE not in fields

    async def test_render_returns_rendering_result(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert isinstance(result, RenderingResult)
        assert result.event_id == "evt-1"
        assert result.target_adapter == "lxmf_node"

    async def test_render_metadata_includes_renderer(self) -> None:
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.metadata["renderer"] == "lxmf"

    async def test_render_very_long_text_no_truncation_in_tranche1(self) -> None:
        renderer = LxmfRenderer()
        long_text = "x" * 1000
        event = _make_event(payload={"body": long_text})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == long_text
        assert result.truncated is False

    async def test_fallback_text_omits_relations_from_envelope(self) -> None:
        """Under fallback_text, the envelope relations list is empty.

        Relations are represented only as inline text in the content
        field, not as structured data in the MEDRE fields envelope.
        """
        renderer = LxmfRenderer(metadata_embedding=True)
        event = _make_event_with_relations()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="fallback_text",
            ),
        )
        fields = result.payload["fields"]
        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        # Envelope relations MUST be empty under fallback_text
        assert envelope["relations"] == []
        # Content text MUST contain the degraded inline relation
        content = result.payload["content"]
        assert "[reply to:" in content

    async def test_direct_strategy_keeps_relations_in_envelope(self) -> None:
        """Under direct strategy, envelope retains structured relations."""
        renderer = LxmfRenderer(metadata_embedding=True)
        event = _make_event_with_relations()
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="direct",
            ),
        )
        fields = result.payload["fields"]
        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        # Envelope relations MUST contain the relation data
        assert len(envelope["relations"]) == 1
        assert envelope["relations"][0]["relation_type"] == "reply"

    async def test_fallback_text_envelope_retains_provenance(self) -> None:
        """Under fallback_text, envelope still has event_id and lineage."""
        renderer = LxmfRenderer(metadata_embedding=True)
        event = _make_event_with_relations(event_id="evt-prov-1")
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="fallback_text",
            ),
        )
        fields = result.payload["fields"]
        envelope = fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["event_id"] == "evt-prov-1"
        assert "lineage" in envelope
        assert envelope["relations"] == []


def _make_reaction_event(
    rel_key: str | None = None,
    payload: dict | None = None,
) -> CanonicalEvent:
    """Create an event with a single reaction relation for emoji fallback tests."""
    return CanonicalEvent(
        event_id="evt-reaction",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="lxmf-1",
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(
            EventRelation(
                relation_type="reaction",
                target_event_id="evt-target",
                target_native_ref=NativeRef(
                    adapter="lxmf-1",
                    native_channel_id=None,
                    native_message_id="msg123",
                ),
                key=rel_key,
                fallback_text="target msg",
            ),
        ),
        payload=payload or {"body": "reaction body"},
        metadata=EventMetadata(),
    )


class TestDegradeRelationsInline:
    """Tests for _degrade_relations_inline reaction emoji fallback resolution."""

    def test_no_relations_returns_text_unchanged(self) -> None:
        """Early-return path: event with no relations returns text as-is."""
        renderer = LxmfRenderer()
        event = _make_event()
        result = renderer._degrade_relations_inline(event, "original text")
        assert result == "original text"

    def test_reaction_emoji_from_rel_key(self) -> None:
        """rel.key takes highest priority for emoji resolution."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(
            rel_key="👍",
            payload={"body": "hi", "key": "❤️", "emoji": "🎉"},
        )
        result = renderer._degrade_relations_inline(event, "msg")
        assert "[reaction 👍 to:" in result

    def test_reaction_emoji_from_payload_key(self) -> None:
        """When rel.key is None, payload['key'] is used."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(
            rel_key=None,
            payload={"body": "hi", "key": "❤️", "emoji": "🎉"},
        )
        result = renderer._degrade_relations_inline(event, "msg")
        assert "[reaction ❤️ to:" in result

    def test_reaction_emoji_from_payload_emoji(self) -> None:
        """When rel.key and payload['key'] are absent, payload['emoji'] is used."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(
            rel_key=None,
            payload={"body": "hi", "emoji": "🎉"},
        )
        result = renderer._degrade_relations_inline(event, "msg")
        assert "[reaction 🎉 to:" in result

    def test_reaction_emoji_hardcoded_fallback(self) -> None:
        """When nothing else is available, hardcoded '∟' fallback is used."""
        renderer = LxmfRenderer()
        event = _make_reaction_event(
            rel_key=None,
            payload={"body": "hi"},
        )
        result = renderer._degrade_relations_inline(event, "msg")
        assert "[reaction ∟ to:" in result
