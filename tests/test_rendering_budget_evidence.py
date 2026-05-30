"""Budget, truncation, and bounded-size evidence tests for renderers.

Covers:
- Byte truncation edge cases (zero budget, multibyte UTF-8, context overrides).
- Raw truncation metadata preservation on RenderingResult.
- MeshCore byte-budget truncation evidence metrics.
- LXMF budget enforcement (char budget, truncation evidence, payload exclusion).
- Evidence size boundedness for huge payloads (no text stored, metrics only).
"""

from __future__ import annotations

import json

from medre.core.events import EventRelation
from medre.core.rendering.evidence import (
    RenderingEvidence,
)
from medre.core.rendering.renderer import (
    RenderingResult,
)
from tests.helpers.rendering_evidence import (
    make_context,
    make_event,
    make_lxmf_renderer,
    make_matrix_renderer,
    make_meshcore_renderer,
    make_meshtastic_renderer,
)

# ===================================================================
# Byte truncation edge cases
# ===================================================================


class TestByteTruncationEdgeCases:
    """Edge cases for byte truncation metadata."""

    async def test_zero_byte_budget_produces_empty_text(self) -> None:
        """Zero max_text_bytes renders empty text with correct metadata."""
        renderer = make_meshcore_renderer(max_text_bytes=0)
        event = make_event(payload={"text": "hello"})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        assert result.payload["text"] == ""
        assert result.truncated is True
        assert isinstance(result.metadata["original_text_bytes"], int)
        assert result.metadata["original_text_bytes"] > 0
        assert result.metadata["rendered_text_bytes"] == 0

    async def test_multibyte_utf8_not_split(self) -> None:
        """UTF-8 multi-byte characters are never split in truncation.

        Uses MeshCore renderer (no prefix formatting) to get clean output.
        """
        text = "🎉" * 50  # 200 bytes
        renderer = make_meshcore_renderer(max_text_bytes=10)
        event = make_event(payload={"text": text})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        # 10 bytes = 2 emojis (8 bytes), 3rd emoji would be 12 > 10
        rendered_text = result.payload["text"]
        assert rendered_text == "🎉" * 2
        assert isinstance(result.metadata["rendered_text_bytes"], int)
        assert result.metadata["rendered_text_bytes"] == 8

    async def test_meshcore_zero_byte_budget(self) -> None:
        """MeshCore with zero byte budget also produces empty text."""
        renderer = make_meshcore_renderer(max_text_bytes=0)
        event = make_event(payload={"text": "data"})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        assert result.payload["text"] == ""
        assert result.truncated is True

    async def test_context_byte_budget_overrides_config(self) -> None:
        """RenderingContext.max_text_bytes takes precedence over config."""
        renderer = make_meshcore_renderer(max_text_bytes=1000)
        event = make_event(payload={"text": "D" * 100})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            max_text_bytes=5,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.metadata["max_text_bytes"] == 5
        assert isinstance(result.metadata["rendered_text_bytes"], int)
        assert result.metadata["rendered_text_bytes"] <= 5


# ===================================================================
# MeshCore byte-budget truncation evidence (item A)
# ===================================================================


class TestMeshCoreByteBudgetTruncationEvidence:
    """MeshCore byte-budget truncation records correct evidence metrics."""

    async def test_meshcore_truncation_evidence_byte_counts(self) -> None:
        """MeshCore evidence reports original_bytes > max_bytes > rendered_bytes."""
        renderer = make_meshcore_renderer(max_text_bytes=20)
        event = make_event(payload={"text": "A" * 100})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        assert evidence.rendered_text_bytes is not None
        assert evidence.rendered_text_bytes <= 20
        assert evidence.original_text_bytes is not None
        assert evidence.original_text_bytes > 20

    async def test_meshcore_no_truncation_evidence(self) -> None:
        """MeshCore evidence reports no truncation when text fits."""
        renderer = make_meshcore_renderer(max_text_bytes=500)
        event = make_event(payload={"text": "short"})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is False
        assert evidence.rendered_text_bytes is not None
        assert evidence.original_text_bytes is not None
        assert evidence.rendered_text_bytes == evidence.original_text_bytes

    async def test_meshcore_context_budget_overrides_config_evidence(self) -> None:
        """MeshCore respects context budget over config, reflected in evidence."""
        renderer = make_meshcore_renderer(max_text_bytes=1000)
        event = make_event(payload={"text": "B" * 100})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            max_text_bytes=10,
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        assert evidence.max_text_bytes == 10
        assert evidence.rendered_text_bytes is not None
        assert evidence.rendered_text_bytes <= 10

    async def test_meshcore_multibyte_truncation_evidence(self) -> None:
        """MeshCore truncation with multi-byte UTF-8 produces safe evidence."""
        text = "🎉" * 50  # 200 bytes
        renderer = make_meshcore_renderer(max_text_bytes=10)
        event = make_event(payload={"text": text})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        # 10 bytes = 2 emojis (8 bytes); 3rd emoji = 12 > 10
        assert evidence.rendered_text_bytes == 8
        assert evidence.original_text_bytes == 200

    async def test_meshcore_zero_budget_evidence(self) -> None:
        """MeshCore zero byte budget produces empty text with correct evidence."""
        renderer = make_meshcore_renderer(max_text_bytes=0)
        event = make_event(payload={"text": "hello"})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        assert evidence.rendered_text_bytes == 0
        assert evidence.original_text_bytes is not None
        assert evidence.original_text_bytes > 0


# ===================================================================
# LXMF budget enforcement (item B)
# ===================================================================


class TestLxmfBudgetEnforcement:
    """LXMF renderer enforces max_text_chars budget declared by adapter."""

    async def test_lxmf_truncates_at_max_text_chars(self) -> None:
        """LXMF renderer truncates content to max_text_chars."""
        renderer = make_lxmf_renderer()
        long_text = "X" * 20000
        event = make_event(payload={"text": long_text})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=16384,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert len(str(result.payload["content"])) == 16384
        assert result.metadata["original_length"] == 20000

    async def test_lxmf_no_truncation_when_under_budget(self) -> None:
        """LXMF renderer does not truncate when text fits within budget."""
        renderer = make_lxmf_renderer()
        short_text = "Short message"
        event = make_event(payload={"text": short_text})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=16384,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is False
        assert result.payload["content"] == short_text
        assert result.metadata["original_length"] == len(short_text)

    async def test_lxmf_no_truncation_without_budget(self) -> None:
        """LXMF renderer does not truncate when max_text_chars is None."""
        renderer = make_lxmf_renderer()
        long_text = "Y" * 20000
        event = make_event(payload={"text": long_text})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=None,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is False
        assert result.payload["content"] == long_text

    async def test_lxmf_truncation_evidence_records_metrics(self) -> None:
        """LXMF truncation evidence records original and rendered metrics."""
        renderer = make_lxmf_renderer()
        long_text = "Z" * 20000
        event = make_event(payload={"text": long_text})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=100,
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="lxmf",
            ctx=ctx,
            result=result,
        )

        assert evidence.truncated is True
        assert evidence.max_text_chars == 100
        assert evidence.original_text_chars == 20000
        assert evidence.rendered_text_chars is not None
        assert evidence.rendered_text_chars == 100

    async def test_lxmf_evidence_no_payload_storage(self) -> None:
        """LXMF evidence dict does not store the rendered or original text."""
        renderer = make_lxmf_renderer()
        long_text = "A" * 20000
        event = make_event(payload={"text": long_text})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=100,
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="lxmf",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        # Evidence stores metrics only, no text content
        for key, value in d.items():
            assert (
                not isinstance(value, str) or len(value) < 50
            ), f"Evidence key {key!r} unexpectedly stores a long string"

    async def test_lxmf_fallback_with_truncation(self) -> None:
        """LXMF fallback_text + truncation both applied correctly."""
        renderer = make_lxmf_renderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target-123",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        event = make_event(
            payload={"text": "R" * 17000},
            relations=(rel,),
        )
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
            max_text_chars=100,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.fallback_applied == "strategy_fallback_text"
        assert len(str(result.payload["content"])) == 100


# ===================================================================
# RenderingEvidence size bounded for huge payloads (item E)
# ===================================================================


class TestEvidenceSizeBounded:
    """RenderingEvidence does not store rendered text payloads — only metrics."""

    def test_evidence_dict_excludes_payload_text(self) -> None:
        """Evidence to_dict never contains the rendered text content."""
        ctx = make_context(target_adapter="a", target_platform="meshtastic")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="a",
            target_channel=None,
            payload={"text": "x" * 100000},
            metadata={
                "renderer": "test",
                "original_length": 100000,
                "original_text_bytes": 100000,
                "rendered_text_bytes": 100000,
            },
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="test",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        # No key should contain the huge text string
        for value in d.values():
            if isinstance(value, str):
                assert (
                    len(value) < 50
                ), f"Evidence dict contains unexpectedly long string: {len(value)}"

    async def test_meshcore_evidence_bounded_for_huge_payload(self) -> None:
        """MeshCore evidence for a 1MB payload is bounded in serialized size."""
        renderer = make_meshcore_renderer(max_text_bytes=100)
        event = make_event(payload={"text": "A" * 1_000_000})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshcore",
            ctx=ctx,
            result=result,
        )
        blob = json.dumps(evidence.to_dict())
        # Evidence blob should be well under 1KB even for 1MB payload
        assert len(blob) < 1024

    async def test_lxmf_evidence_bounded_for_huge_payload(self) -> None:
        """LXMF evidence for a huge payload is bounded in serialized size."""
        renderer = make_lxmf_renderer()
        event = make_event(payload={"text": "B" * 1_000_000})
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=16384,
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="lxmf",
            ctx=ctx,
            result=result,
        )
        blob = json.dumps(evidence.to_dict())
        assert len(blob) < 1024

    async def test_meshtastic_evidence_bounded_for_huge_payload(self) -> None:
        """Meshtastic evidence for a huge payload is bounded in serialized size."""
        renderer = make_meshtastic_renderer(max_text_bytes=227)
        event = make_event(payload={"text": "C" * 1_000_000})
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        blob = json.dumps(evidence.to_dict())
        assert len(blob) < 1024

    async def test_matrix_evidence_bounded_for_huge_payload(self) -> None:
        """Matrix evidence for a huge payload is bounded in serialized size."""
        renderer = make_matrix_renderer()
        event = make_event(payload={"text": "D" * 1_000_000})
        ctx = make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
            max_text_chars=100,
        )
        result = await renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        blob = json.dumps(evidence.to_dict())
        assert len(blob) < 1024

    def test_evidence_records_metrics_not_payload(self) -> None:
        """Evidence stores character/byte counts, not the text itself."""
        evidence = RenderingEvidence(
            schema_version="1",
            renderer="test",
            target_adapter="a",
            target_platform=None,
            delivery_strategy="direct",
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            truncated=True,
            fallback_applied=None,
            rendered_text_chars=50,
            rendered_text_bytes=50,
            original_text_chars=100000,
            original_text_bytes=100000,
        )
        d = evidence.to_dict()
        # All values should be simple JSON types (str, int, bool, None)
        for key, value in d.items():
            assert isinstance(
                value, (str, int, bool, type(None))
            ), f"Evidence key {key!r} has unexpected type {type(value).__name__}"
        # Specific metric fields are ints, not strings
        assert isinstance(d["rendered_text_chars"], int)
        assert isinstance(d["rendered_text_bytes"], int)
        assert isinstance(d["original_text_chars"], int)
        assert isinstance(d["original_text_bytes"], int)


# ===================================================================
# Truncation metadata preservation on RenderingResult
# ===================================================================


class TestRenderingResultTruncationMetadata:
    """Byte-budget renderers record truncation metadata in their result."""

    async def test_meshtastic_truncation_metadata_present(self) -> None:
        """Meshtastic renderer includes byte counts and truncated flag."""
        renderer = make_meshtastic_renderer(max_text_bytes=227)
        event = make_event(payload={"text": "short"})
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)

        meta = result.metadata
        assert "original_text_bytes" in meta
        assert "rendered_text_bytes" in meta
        assert "max_text_bytes" in meta
        assert "truncated" in meta
        assert isinstance(meta["truncated"], bool)
        assert meta["max_text_bytes"] == 227

    async def test_meshtastic_truncation_records_actual_truncation(self) -> None:
        """When text exceeds byte budget, truncation metadata reports it."""
        renderer = make_meshtastic_renderer(max_text_bytes=10)
        event = make_event(payload={"text": "A" * 100})
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.metadata["truncated"] is True
        # original_text_bytes includes any renderer-applied formatting
        assert isinstance(result.metadata["original_text_bytes"], int)
        assert result.metadata["original_text_bytes"] > 0
        assert isinstance(result.metadata["rendered_text_bytes"], int)
        assert result.metadata["rendered_text_bytes"] <= 10

    async def test_meshcore_truncation_metadata_present(self) -> None:
        """MeshCore renderer includes byte counts and truncated flag."""
        renderer = make_meshcore_renderer(max_text_bytes=512)
        event = make_event(payload={"text": "hello meshcore"})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        meta = result.metadata
        assert "original_text_bytes" in meta
        assert "rendered_text_bytes" in meta
        assert "max_text_bytes" in meta
        assert "truncated" in meta

    async def test_meshcore_truncation_records_actual_truncation(self) -> None:
        """When MeshCore text exceeds byte budget, metadata reports it."""
        renderer = make_meshcore_renderer(max_text_bytes=5)
        event = make_event(payload={"text": "B" * 50})
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.metadata["truncated"] is True
        assert isinstance(result.metadata["rendered_text_bytes"], int)
        assert result.metadata["rendered_text_bytes"] <= 5

    async def test_no_truncation_when_text_fits(self) -> None:
        """When text fits within budget, truncated is False."""
        renderer = make_meshtastic_renderer(max_text_bytes=500)
        event = make_event(payload={"text": "small message"})
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is False
        assert result.metadata["truncated"] is False

    async def test_byte_budget_from_context_overrides_config(self) -> None:
        """RenderingContext.max_text_bytes overrides the adapter config budget."""
        renderer = make_meshtastic_renderer(max_text_bytes=500)
        event = make_event(payload={"text": "C" * 100})
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            max_text_bytes=10,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.metadata["max_text_bytes"] == 10
