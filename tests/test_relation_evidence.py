"""Focused tests for relation/native-target evidence visibility.

Covers:
- RelationTargetEvidence model construction, immutability, and serialization.
- Render mode derivation from delivery_strategy, capability_level, fallback_applied.
- Target availability derivation from relation data.
- Conversation_id / root_event_id propagation from events.
- Pipeline integration: RenderingPipeline.render() populates relation evidence.
- Backward compatibility: from_context_and_result without event produces defaults.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    RelationTargetEvidence,
    RenderingEvidence,
    _derive_relation_render_mode,
)
from medre.core.rendering.renderer import (
    FallbackApplied,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from tests.helpers.rendering_evidence import make_context, make_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_TS = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_relation(
    relation_type: str = "reply",
    target_event_id: str | None = "evt-target-1",
    target_native_ref: NativeRef | None = None,
    fallback_text: str | None = None,
) -> EventRelation:
    return EventRelation(
        relation_type=relation_type,  # type: ignore[arg-type]
        target_event_id=target_event_id,
        target_native_ref=target_native_ref,
        key=None,
        fallback_text=fallback_text,
    )


def _make_event_with_relations(
    event_id: str = "evt-rel-1",
    relations: tuple[EventRelation, ...] = (),
    conversation_id: str | None = None,
    root_event_id: str | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=_FIXED_TS,
        source_adapter="src-adapter",
        source_transport_id="transport-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"text": "relation evidence test"},
        metadata=EventMetadata(),
        conversation_id=conversation_id,
        root_event_id=root_event_id,
    )


def _native_ref(
    adapter: str = "target-adapter",
    native_message_id: str = "native-msg-1",
) -> NativeRef:
    return NativeRef(
        adapter=adapter,
        native_channel_id="ch-0",
        native_message_id=native_message_id,
    )


# ===================================================================
# RelationTargetEvidence model
# ===================================================================


class TestRelationTargetEvidenceModel:
    """Tests for the RelationTargetEvidence model itself."""

    def test_construction_and_fields(self) -> None:
        evidence = RelationTargetEvidence(
            relation_type="reply",
            render_mode="native",
            target_event_id="evt-1",
            target_native_message_id="msg-1",
            target_available=True,
            fallback_text_source=None,
        )
        assert evidence.relation_type == "reply"
        assert evidence.render_mode == "native"
        assert evidence.target_event_id == "evt-1"
        assert evidence.target_native_message_id == "msg-1"
        assert evidence.target_available is True
        assert evidence.fallback_text_source is None

    def test_frozen(self) -> None:
        evidence = RelationTargetEvidence(
            relation_type="reply",
            render_mode="fallback",
            target_event_id=None,
            target_native_message_id=None,
            target_available=None,
            fallback_text_source="some text",
        )
        with pytest.raises(AttributeError):
            evidence.render_mode = "native"  # type: ignore[misc]

    def test_to_dict_json_safe(self) -> None:
        evidence = RelationTargetEvidence(
            relation_type="reaction",
            render_mode="fallback",
            target_event_id="evt-react",
            target_native_message_id=None,
            target_available=True,
            fallback_text_source="👍",
        )
        d = evidence.to_dict()
        serialized = json.dumps(d, sort_keys=True)
        parsed = json.loads(serialized)

        assert parsed["relation_type"] == "reaction"
        assert parsed["render_mode"] == "fallback"
        assert parsed["target_event_id"] == "evt-react"
        assert parsed["target_native_message_id"] is None
        assert parsed["target_available"] is True
        assert parsed["fallback_text_source"] == "👍"

    def test_to_dict_all_none(self) -> None:
        evidence = RelationTargetEvidence(
            relation_type="edit",
            render_mode="native",
            target_event_id=None,
            target_native_message_id=None,
            target_available=None,
            fallback_text_source=None,
        )
        d = evidence.to_dict()
        assert d["target_event_id"] is None
        assert d["target_native_message_id"] is None
        assert d["target_available"] is None
        assert d["fallback_text_source"] is None


# ===================================================================
# Render mode derivation
# ===================================================================


class TestRenderModeDerivation:
    """Tests for _derive_relation_render_mode logic."""

    def test_direct_strategy_native_capability_yields_native(self) -> None:
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "native"

    def test_fallback_text_strategy_yields_fallback(self) -> None:
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="fallback_text",
            capability_level="native",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "fallback"

    def test_fallback_capability_yields_fallback(self) -> None:
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="fallback",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "fallback"

    def test_unsupported_capability_yields_fallback(self) -> None:
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="unsupported",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "fallback"

    def test_relation_specific_fallback_applied_matching(self) -> None:
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied="relation_reply",
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "fallback"

    def test_relation_specific_fallback_applied_non_matching(self) -> None:
        """fallback_applied='relation_reaction' does not affect reply."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied="relation_reaction",
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "native"

    def test_relation_fallback_for_each_type(self) -> None:
        for rtype in ("reply", "reaction", "edit", "delete", "thread"):
            fb: FallbackApplied = f"relation_{rtype}"  # type: ignore[assignment]
            mode = _derive_relation_render_mode(
                relation_type=rtype,
                delivery_strategy="direct",
                capability_level="native",
                fallback_applied=fb,
                target_event_id="evt-target-1",
                target_native_message_id="msg-1",
            )
            assert mode == "fallback", f"Expected fallback for {rtype}"

    def test_strategy_fallback_text_not_relation_type(self) -> None:
        """strategy_fallback_text does not match any relation type suffix."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied="strategy_fallback_text",
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "native"

    def test_no_target_event_id_yields_fallback(self) -> None:
        """Even with native strategy/capability, no target_event_id → fallback."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
            target_event_id=None,
        )
        assert mode == "fallback"

    def test_no_native_message_id_yields_fallback(self) -> None:
        """target_event_id present but no usable native_message_id → fallback."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id=None,
        )
        assert mode == "fallback"

    def test_empty_native_message_id_yields_fallback(self) -> None:
        """target_event_id present but empty native_message_id → fallback."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="",
        )
        assert mode == "fallback"

    def test_usable_native_ref_with_native_strategy_yields_native(self) -> None:
        """Both target_event_id and truthy native_message_id → native."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
            target_event_id="evt-target-1",
            target_native_message_id="msg-1",
        )
        assert mode == "native"

    def test_no_target_default_params_yields_fallback(self) -> None:
        """Omitting target params (defaults to None) → fallback."""
        mode = _derive_relation_render_mode(
            relation_type="reply",
            delivery_strategy="direct",
            capability_level="native",
            fallback_applied=None,
        )
        assert mode == "fallback"


# ===================================================================
# Target availability derivation
# ===================================================================


class TestTargetAvailability:
    """Tests for target_available field derivation in evidence."""

    def test_target_event_id_present_yields_true(self) -> None:
        event = _make_event_with_relations(
            relations=(_make_relation(target_event_id="evt-resolved"),),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert len(evidence.relation_evidence) == 1
        assert evidence.relation_evidence[0].target_available is True

    def test_target_event_id_none_yields_none(self) -> None:
        event = _make_event_with_relations(
            relations=(_make_relation(target_event_id=None),),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert len(evidence.relation_evidence) == 1
        assert evidence.relation_evidence[0].target_available is None

    def test_native_ref_provides_message_id(self) -> None:
        event = _make_event_with_relations(
            relations=(
                _make_relation(
                    target_event_id="evt-t",
                    target_native_ref=_native_ref(
                        adapter="other-adapter",
                        native_message_id="msg-xyz",
                    ),
                ),
            ),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        rel_ev = evidence.relation_evidence[0]
        assert rel_ev.target_native_message_id == "msg-xyz"
        assert rel_ev.target_available is True

    def test_no_native_ref_yields_none_message_id(self) -> None:
        event = _make_event_with_relations(
            relations=(_make_relation(target_native_ref=None),),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert evidence.relation_evidence[0].target_native_message_id is None


# ===================================================================
# Conversation / root_event_id propagation
# ===================================================================


class TestConversationRootEventId:
    """Tests for conversation_id and root_event_id in evidence."""

    def test_event_with_conversation_id(self) -> None:
        event = _make_event_with_relations(
            conversation_id="conv-123",
            root_event_id="root-evt-1",
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert evidence.conversation_id == "conv-123"
        assert evidence.root_event_id == "root-evt-1"

    def test_event_without_conversation_id(self) -> None:
        event = _make_event_with_relations()
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert evidence.conversation_id is None
        assert evidence.root_event_id is None

    def test_no_event_yields_none(self) -> None:
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
        )
        assert evidence.conversation_id is None
        assert evidence.root_event_id is None

    def test_conversation_id_in_to_dict(self) -> None:
        event = _make_event_with_relations(
            conversation_id="conv-abc",
            root_event_id="root-def",
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        d = evidence.to_dict()
        assert d["conversation_id"] == "conv-abc"
        assert d["root_event_id"] == "root-def"


# ===================================================================
# Multi-relation evidence
# ===================================================================


class TestMultiRelationEvidence:
    """Tests for events with multiple relations producing per-relation evidence."""

    def test_multiple_relations_each_get_evidence(self) -> None:
        event = _make_event_with_relations(
            relations=(
                _make_relation(
                    relation_type="reply",
                    target_event_id="evt-reply-target",
                    fallback_text="original message",
                ),
                _make_relation(
                    relation_type="reaction",
                    target_event_id="evt-react-target",
                ),
            ),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert len(evidence.relation_evidence) == 2

        reply_ev = evidence.relation_evidence[0]
        assert reply_ev.relation_type == "reply"
        assert reply_ev.target_event_id == "evt-reply-target"
        assert reply_ev.fallback_text_source == "original message"

        react_ev = evidence.relation_evidence[1]
        assert react_ev.relation_type == "reaction"
        assert react_ev.target_event_id == "evt-react-target"

    def test_mixed_native_fallback_per_relation(self) -> None:
        """fallback_applied='relation_reply' makes reply fallback but not reaction."""
        event = _make_event_with_relations(
            relations=(
                _make_relation(relation_type="reply", target_event_id="evt-1"),
                _make_relation(
                    relation_type="reaction",
                    target_event_id="evt-2",
                    target_native_ref=_native_ref(
                        adapter="src-adapter",
                        native_message_id="msg-react-1",
                    ),
                ),
            ),
        )
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
            fallback_applied="relation_reply",
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert evidence.relation_evidence[0].render_mode == "fallback"
        assert evidence.relation_evidence[1].render_mode == "native"

    def test_all_fallback_via_strategy(self) -> None:
        event = _make_event_with_relations(
            relations=(
                _make_relation(relation_type="reply", target_event_id="evt-1"),
                _make_relation(relation_type="edit", target_event_id="evt-2"),
            ),
        )
        ctx = make_context(
            target_adapter="target-1",
            delivery_strategy="fallback_text",
        )
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "degraded"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        for rel_ev in evidence.relation_evidence:
            assert rel_ev.render_mode == "fallback"

    def test_empty_relations_yields_empty_evidence(self) -> None:
        event = _make_event_with_relations(relations=())
        ctx = make_context(target_adapter="target-1")
        result = RenderingResult(
            event_id="evt-rel-1",
            target_adapter="target-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
            event=event,
        )
        assert evidence.relation_evidence == ()


# ===================================================================
# Pipeline integration
# ===================================================================


class TestPipelineRelationEvidence:
    """RenderingPipeline.render() populates relation evidence from the event."""

    async def test_pipeline_populates_relation_evidence(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event_with_relations(
            relations=(
                _make_relation(
                    relation_type="reply",
                    target_event_id="evt-reply-target",
                ),
            ),
        )
        result = await pipeline.render(
            event,
            target_adapter="target-1",
        )
        assert result.rendering_evidence is not None
        evidence = result.rendering_evidence
        assert len(evidence.relation_evidence) == 1
        assert evidence.relation_evidence[0].relation_type == "reply"
        assert evidence.relation_evidence[0].target_event_id == "evt-reply-target"
        # TextRenderer degrades relations to plain text → fallback mode.
        assert evidence.relation_evidence[0].render_mode == "fallback"

    async def test_pipeline_populates_conversation_id(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event_with_relations(
            conversation_id="conv-pipeline",
            root_event_id="root-pipeline",
        )
        result = await pipeline.render(
            event,
            target_adapter="target-1",
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.conversation_id == "conv-pipeline"
        assert result.rendering_evidence.root_event_id == "root-pipeline"

    async def test_pipeline_no_relations_empty_evidence(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = make_event(event_id="evt-no-rels")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.relation_evidence == ()

    async def test_pipeline_evidence_serializable_with_relations(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event_with_relations(
            relations=(
                _make_relation(
                    relation_type="reply",
                    target_event_id="evt-target",
                    target_native_ref=_native_ref(
                        adapter="src-adapter",
                        native_message_id="msg-42",
                    ),
                    fallback_text="original msg text",
                ),
            ),
            conversation_id="conv-serial",
            root_event_id="root-serial",
        )
        result = await pipeline.render(
            event,
            target_adapter="target-1",
        )
        assert result.rendering_evidence is not None
        d = result.rendering_evidence.to_dict()

        # Full round-trip through JSON.
        serialized = json.dumps(d, sort_keys=True)
        parsed = json.loads(serialized)

        assert parsed["conversation_id"] == "conv-serial"
        assert parsed["root_event_id"] == "root-serial"
        assert len(parsed["relation_evidence"]) == 1

        rel = parsed["relation_evidence"][0]
        assert rel["relation_type"] == "reply"
        # TextRenderer degrades relations → fallback.
        assert rel["render_mode"] == "fallback"
        assert rel["target_event_id"] == "evt-target"
        assert rel["target_native_message_id"] == "msg-42"
        assert rel["target_available"] is True
        assert rel["fallback_text_source"] == "original msg text"


# ===================================================================
# Backward compatibility
# ===================================================================


class TestBackwardCompatibility:
    """Existing code that constructs evidence without event still works."""

    def test_from_context_and_result_without_event(self) -> None:
        ctx = make_context(target_adapter="adapter-1")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="adapter-1",
            target_channel=None,
            payload={"text": "hello"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="text",
            ctx=ctx,
            result=result,
        )
        assert evidence.conversation_id is None
        assert evidence.root_event_id is None
        assert evidence.relation_evidence == ()

    def test_direct_construction_defaults(self) -> None:
        evidence = RenderingEvidence(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            renderer="text",
            delivery_strategy="direct",
            target_adapter="a",
            target_platform=None,
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            fallback_applied=None,
            truncated=False,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=5,
            original_text_bytes=None,
        )
        assert evidence.conversation_id is None
        assert evidence.root_event_id is None
        assert evidence.relation_evidence == ()
        d = evidence.to_dict()
        assert d["conversation_id"] is None
        assert d["root_event_id"] is None
        assert d["relation_evidence"] == []

    def test_schema_version_frozen_at_one(self) -> None:
        assert EVIDENCE_SCHEMA_VERSION == "1"

    def test_to_dict_shape_includes_new_keys(self) -> None:
        evidence = RenderingEvidence(
            schema_version="1",
            renderer="text",
            delivery_strategy="direct",
            target_adapter="a",
            target_platform=None,
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            fallback_applied=None,
            truncated=False,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=5,
            original_text_bytes=None,
        )
        d = evidence.to_dict()
        assert "conversation_id" in d
        assert "root_event_id" in d
        assert "relation_evidence" in d
