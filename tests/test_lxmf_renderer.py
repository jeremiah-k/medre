"""Tests for LxmfRenderer: name, can_render dispatch, rendering output,
title, fields envelope, relay prefix, target-aware configs, and edge cases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.events.metadata import EventMetadata as _EM
from medre.core.events.metadata import NativeMetadata
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from medre.runtime.builder import SourceAttributionConfig


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


class TestLxmfTargetSelectionRules:
    """Lock the target-selection contracts for LxmfRenderer.

    These tests assert the *current* behaviour — guards against accidental
    changes, not aspirational specifications.

    Key contracts:
    - LxmfRenderer has **no** native relation rendering.  No transport-
      specific relation fields (reply_id, emoji, m.relates_to) are ever
      emitted in the payload.
    - All relations are degraded to inline text via
      ``degrade_relations_inline`` which iterates **all** relations, not
      just ``relations[0]``.
    - In ``direct`` strategy, relations are kept as structured data in the
      MEDRE fields envelope; in ``fallback_text`` strategy, relations are
      degraded to inline text and the envelope carries an empty list.
    - The payload always contains ``content``, ``title``, ``fields``,
      and ``destination_hash`` — never ``reply_id``, ``emoji``, or
      ``m.relates_to``.
    """

    async def test_all_relations_degraded_inline_under_fallback(self) -> None:
        """All relations appear in degraded inline text under fallback_text.

        LXMF's ``degrade_relations_inline`` iterates every relation,
        unlike Matrix/Meshtastic which only inspect ``relations[0]``.
        """
        renderer = LxmfRenderer(metadata_embedding=False)
        reply_rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-reply-target",
            target_native_ref=None,
            key=None,
            fallback_text="original msg",
        )
        reaction_rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-react-target",
            target_native_ref=None,
            key="👍",
            fallback_text="reacted msg",
        )
        event = CanonicalEvent(
            event_id="evt-multi-rel",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(reply_rel, reaction_rel),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="fallback_text",
            ),
        )
        content = result.payload["content"]
        # Both relations must appear in degraded inline text
        assert "[reply to:" in content
        assert "[reaction 👍 to:" in content

    async def test_no_native_target_fields_ever_emitted(self) -> None:
        """LXMF payload never contains native relation fields.

        Regardless of relation type or native ref presence, the payload
        contains only content, title, fields, and destination_hash.
        """
        renderer = LxmfRenderer(metadata_embedding=False)
        native_ref = NativeRef(
            adapter="lxmf-1",
            native_channel_id=None,
            native_message_id="abc123",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-001",
            target_native_ref=native_ref,
            key=None,
            fallback_text="original",
        )
        event = CanonicalEvent(
            event_id="evt-no-native",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        payload_keys = set(result.payload.keys())
        # Only these keys are ever emitted
        assert payload_keys == {"content", "title", "fields", "destination_hash"}
        # Explicitly no native relation fields
        assert "reply_id" not in result.payload
        assert "emoji" not in result.payload
        assert "m.relates_to" not in result.payload

    async def test_direct_strategy_preserves_structured_relations(self) -> None:
        """Under direct strategy, structured relations are preserved in the
        MEDRE fields envelope.  No inline degradation occurs."""
        renderer = LxmfRenderer(metadata_embedding=True)
        native_ref = NativeRef(
            adapter="lxmf-1",
            native_channel_id=None,
            native_message_id="msg456",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target",
            target_native_ref=native_ref,
            key=None,
            fallback_text="target text",
        )
        event = CanonicalEvent(
            event_id="evt-direct-rel",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        # Content is plain text — no inline degradation
        assert result.payload["content"] == "hello"
        # Structured relations preserved in envelope
        fields = result.payload["fields"]
        envelope = fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert len(envelope["relations"]) == 1
        assert envelope["relations"][0]["relation_type"] == "reply"


# ---------------------------------------------------------------------------
# Relay prefix tests
# ---------------------------------------------------------------------------


def _make_event_with_native(
    source_adapter: str = "matrix-1",
    native_data: dict | None = None,
    payload: dict | None = None,
) -> CanonicalEvent:
    """Create an event with native metadata for prefix extraction tests."""
    metadata = _EM(native=NativeMetadata(data=native_data or {}))
    return CanonicalEvent(
        event_id="evt-prefix-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="ab" * 16,
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"body": "hello world"},
        metadata=metadata,
    )


class TestLxmfRelayPrefix:
    """Relay prefix prepended to human-readable body text."""

    async def test_empty_prefix_preserves_body(self) -> None:
        """Default empty prefix leaves content unchanged."""
        renderer = LxmfRenderer(relay_prefix="")
        event = _make_event(payload={"body": "original text"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "original text"
        assert "relay_prefix_template" not in result.metadata

    async def test_matrix_prefix_with_display_name(self) -> None:
        """Matrix -> LXMF prefix uses display name from native metadata."""
        renderer = LxmfRenderer(relay_prefix="[{source_display_name}] ")
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@alice:example.com",
                "displayname": "Alice",
            },
            payload={"body": "hello from matrix"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "[Alice] hello from matrix"
        assert result.metadata["relay_prefix_rendered"] == "[Alice] "

    async def test_meshtastic_prefix_with_sender_short(self) -> None:
        """Meshtastic -> LXMF prefix uses sender_short from native metadata."""
        renderer = LxmfRenderer(relay_prefix="<{sender_short}> ")
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "Base Station",
                "shortname": "BASE",
                "from_id": "!a1b2c3d4",
            },
            payload={"body": "radio check"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "<BASE> radio check"
        assert result.metadata["relay_prefix_rendered"] == "<BASE> "

    async def test_meshtastic_prefix_with_sender_id(self) -> None:
        """Meshtastic -> LXMF prefix uses sender_id."""
        renderer = LxmfRenderer(relay_prefix="({sender_id}) ")
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={
                "from_id": "!a1b2c3d4",
            },
            payload={"body": "ping"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "(!a1b2c3d4) ping"

    async def test_meshcore_prefix_with_pubkey_fallback(self) -> None:
        """MeshCore -> LXMF prefix uses pubkey as sender_id fallback."""
        renderer = LxmfRenderer(relay_prefix="{source_sender_id}: ")
        event = _make_event_with_native(
            source_adapter="meshcore-node",
            native_data={
                "pubkey_prefix": "4a2f8c",
            },
            payload={"body": "mesh message"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "4a2f8c: mesh message"
        assert result.metadata["relay_prefix_rendered"] == "4a2f8c: "

    async def test_missing_vars_no_none_in_output(self) -> None:
        """Missing attribution variables produce empty string, not 'None'."""
        renderer = LxmfRenderer(relay_prefix="[{source_display_name}] ")
        # Event with no native metadata — display_name will be empty
        event = _make_event_with_native(
            source_adapter="unknown-source",
            native_data={},
            payload={"body": "mystery msg"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        # Prefix resolves to empty since display_name is missing
        assert result.payload["content"] == "[] mystery msg"
        assert "None" not in result.payload["content"]

    async def test_prefix_with_fallback_text_strategy(self) -> None:
        """Prefix prepended before body; degraded relations appended after."""
        renderer = LxmfRenderer(
            metadata_embedding=True,
            relay_prefix="[{source_display_name}] ",
        )
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-original",
            target_native_ref=NativeRef(
                adapter="lxmf-1",
                native_channel_id=None,
                native_message_id="abc123",
            ),
            key=None,
            fallback_text="original text",
        )
        event = CanonicalEvent(
            event_id="evt-prefix-fb",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="matrix-bridge",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(rel,),
            payload={"body": "my reply"},
            metadata=_EM(
                native=NativeMetadata(
                    data={"sender": "@bob:example.com", "displayname": "Bob"}
                )
            ),
        )
        result = await renderer.render(
            event,
            RenderingContext(
                target_adapter="lxmf_node",
                delivery_strategy="fallback_text",
            ),
        )
        content = result.payload["content"]
        # Prefix comes first, then body, then degraded relations
        assert content.startswith("[Bob] my reply")
        assert "[reply to:" in content
        # Envelope still has empty relations (fallback_text contract)
        envelope = result.payload["fields"][FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["relations"] == []

    async def test_prefix_metadata_in_result(self) -> None:
        """Result metadata records prefix template and rendered string."""
        renderer = LxmfRenderer(relay_prefix="<{sender_short}> ")
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={"shortname": "DEV"},
            payload={"body": "test"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.metadata["relay_prefix_template"] == "<{sender_short}> "
        assert result.metadata["relay_prefix_rendered"] == "<DEV> "

    async def test_prefix_metadata_absent_when_empty(self) -> None:
        """No prefix metadata keys when prefix is empty (default)."""
        renderer = LxmfRenderer()
        event = _make_event()
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "relay_prefix_template" not in result.metadata
        assert "relay_prefix_rendered" not in result.metadata
        assert "relay_prefix_variables_used" not in result.metadata
        assert "relay_prefix_missing_variables" not in result.metadata
        assert "relay_prefix_unknown_variables" not in result.metadata
        assert "relay_prefix_formatting_error" not in result.metadata

    async def test_prefix_formatting_error_recorded(self) -> None:
        """Unknown placeholder triggers formatting_error in metadata."""
        renderer = LxmfRenderer(relay_prefix="{bogus_var} ")
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"displayname": "Test"},
            payload={"body": "hello"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert "relay_prefix_formatting_error" in result.metadata
        assert "bogus_var" in str(result.metadata["relay_prefix_formatting_error"])
        # Unknown placeholder left unchanged in rendered prefix
        assert "{bogus_var}" in result.metadata["relay_prefix_rendered"]

    async def test_prefix_does_not_duplicate_envelope(self) -> None:
        """Prefix is human-readable only; metadata envelope is separate."""
        renderer = LxmfRenderer(
            metadata_embedding=True,
            relay_prefix="[{source_display_name}] ",
        )
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@carol:example.com",
                "displayname": "Carol",
            },
            payload={"body": "test envelope"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        # Content has the prefix
        assert result.payload["content"] == "[Carol] test envelope"
        # Envelope still exists and has event_id
        fields = result.payload["fields"]
        assert FIELD_MEDRE_ENVELOPE in fields
        envelope = fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
        assert envelope["event_id"] == "evt-prefix-1"
        # Envelope does NOT contain the prefix or rendered text
        assert "relay_prefix" not in envelope


class TestLxmfTargetAwareConfigs:
    """Target-aware config resolution for LxmfRenderer.

    Tests that the renderer resolves prefix from the target adapter's
    config mapping at render time, not from a single global prefix.
    """

    async def test_prefix_from_target_adapter_config(self) -> None:
        """Prefix resolved from configs mapping via target_adapter."""
        configs = {
            "lxmf_a": LxmfConfig(
                adapter_id="lxmf_a",
                connection_type="fake",
                lxmf_relay_prefix="[{source_display_name}] ",
            ),
        }
        renderer = LxmfRenderer(configs=configs)
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={
                "sender": "@alice:example.com",
                "displayname": "Alice",
            },
            payload={"body": "hello"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_a", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "[Alice] hello"

    async def test_two_lxmf_adapters_different_prefixes(self) -> None:
        """Two LXMF adapters with different prefixes render correctly."""
        configs = {
            "lxmf_alpha": LxmfConfig(
                adapter_id="lxmf_alpha",
                connection_type="fake",
                lxmf_relay_prefix="[{source_display_name}] ",
            ),
            "lxmf_beta": LxmfConfig(
                adapter_id="lxmf_beta",
                connection_type="fake",
                lxmf_relay_prefix="<{sender_short}> ",
            ),
        }
        renderer = LxmfRenderer(configs=configs)
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={
                "longname": "Base Radio",
                "shortname": "RAD",
            },
            payload={"body": "hello"},
        )

        # Render to lxmf_alpha — uses display_name (longname) prefix
        result_a = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_alpha", delivery_strategy="direct"),
        )
        assert result_a.payload["content"] == "[Base Radio] hello"

        # Render to lxmf_beta — uses sender_short prefix
        result_b = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_beta", delivery_strategy="direct"),
        )
        assert result_b.payload["content"] == "<RAD> hello"

    async def test_empty_prefix_from_config(self) -> None:
        """Adapter config with empty lxmf_relay_prefix produces no prefix."""
        configs = {
            "lxmf_plain": LxmfConfig(
                adapter_id="lxmf_plain",
                connection_type="fake",
                lxmf_relay_prefix="",
            ),
        }
        renderer = LxmfRenderer(configs=configs)
        event = _make_event(payload={"body": "plain text"})
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_plain", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "plain text"
        assert "relay_prefix_template" not in result.metadata

    async def test_fallback_to_relay_prefix_when_no_configs(self) -> None:
        """relay_prefix fallback used when configs mapping is empty."""
        renderer = LxmfRenderer(relay_prefix="[{source_display_name}] ")
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"displayname": "Alice"},
            payload={"body": "hello"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "[Alice] hello"

    async def test_fallback_to_relay_prefix_when_target_not_in_configs(self) -> None:
        """relay_prefix fallback used when target_adapter not in configs."""
        configs = {
            "lxmf_other": LxmfConfig(
                adapter_id="lxmf_other",
                connection_type="fake",
                lxmf_relay_prefix="<{sender_short}> ",
            ),
        }
        renderer = LxmfRenderer(
            configs=configs,
            relay_prefix="[{source_display_name}] ",
        )
        event = _make_event_with_native(
            source_adapter="matrix-bridge",
            native_data={"displayname": "Alice"},
            payload={"body": "hello"},
        )
        # Target adapter is NOT in configs — falls back to relay_prefix
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_unknown", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "[Alice] hello"


class TestLxmfSourceOriginLabel:
    """origin_label from source_attribution registry appears in prefix."""

    async def test_origin_label_from_source_attribution(self) -> None:
        """Source origin_label from registry is used in prefix formatting."""
        source_attr = {
            "meshtastic-radio": SourceAttributionConfig(
                adapter_id="meshtastic-radio",
                platform="meshtastic",
                origin_label="East Radio",
            ),
        }
        renderer = LxmfRenderer(
            relay_prefix="[{origin_label}] ",
            source_attribution=source_attr,
        )
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={},
            payload={"body": "hello"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "[East Radio] hello"

    async def test_origin_label_with_configs_mapping(self) -> None:
        """origin_label works with target-aware configs mapping."""
        source_attr = {
            "meshtastic-radio": SourceAttributionConfig(
                adapter_id="meshtastic-radio",
                platform="meshtastic",
                origin_label="Base Station Alpha",
            ),
        }
        configs = {
            "lxmf_a": LxmfConfig(
                adapter_id="lxmf_a",
                connection_type="fake",
                lxmf_relay_prefix="<{origin_label}> ",
            ),
        }
        renderer = LxmfRenderer(
            configs=configs,
            source_attribution=source_attr,
        )
        event = _make_event_with_native(
            source_adapter="meshtastic-radio",
            native_data={},
            payload={"body": "radio check"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_a", delivery_strategy="direct"),
        )
        assert result.payload["content"] == "<Base Station Alpha> radio check"

    async def test_no_origin_label_when_not_in_registry(self) -> None:
        """Empty origin_label when source adapter not in registry."""
        renderer = LxmfRenderer(
            relay_prefix="[{origin_label}] ",
            source_attribution={},
        )
        event = _make_event_with_native(
            source_adapter="unknown-source",
            native_data={},
            payload={"body": "mystery"},
        )
        result = await renderer.render(
            event,
            RenderingContext(target_adapter="lxmf_node", delivery_strategy="direct"),
        )
        # origin_label resolves to empty string
        assert result.payload["content"] == "[] mystery"
