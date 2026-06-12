"""Rendering and evidence conformance tests.

Asserts that renderers produce correct native payloads and that
RenderingEvidence captures the right decision inputs and metrics.
Covers:

* Matrix direct text renders native envelope (msgtype, body).
* Matrix reply native relation includes m.relates_to.
* Matrix fallback_text reply omits m.relates_to and preserves body.
* Meshtastic direct text includes text, channel_index.
* Meshtastic fallback_text reply preserves channel_index.
* Meshtastic byte-budget truncation has correct byte limit and evidence.
* RenderingEvidence includes renderer, target_adapter, target_platform,
  delivery_strategy, capability_level, text char/byte metrics,
  truncation flag.
"""

from __future__ import annotations

import json

import pytest

from medre.core.rendering.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    RenderingEvidence,
)
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
)

from .conftest import (
    MATRIX_ADAPTER_ID,
    MESHTASTIC_ADAPTER_ID,
    make_reply_event,
    make_text_event,
)

# ---------------------------------------------------------------------------
# Matrix rendering conformance
# ---------------------------------------------------------------------------


class TestMatrixRenderingConformance:
    """Assert Matrix renderer output matches MEDRE rendering contracts."""

    @pytest.mark.asyncio
    async def test_matrix_direct_text_renders_native_envelope(self, matrix_renderer):
        """Matrix direct text renders msgtype=m.text and body."""
        event = make_text_event(source_adapter="matrix_conf", body="Hello Matrix")
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        assert result.payload.get("msgtype") == "m.text"
        assert result.payload.get("body") == "Hello Matrix"

    @pytest.mark.asyncio
    async def test_matrix_direct_reply_includes_m_relates_to(self, matrix_renderer):
        """Matrix direct reply renders m.relates_to.m.in_reply_to."""
        event = make_reply_event(
            source_adapter=MATRIX_ADAPTER_ID,
            target_adapter=MATRIX_ADAPTER_ID,
            target_message_id="$orig_001",
        )
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        relates = result.payload.get("m.relates_to")
        assert relates is not None
        assert "m.in_reply_to" in relates
        assert relates["m.in_reply_to"]["event_id"] == "$orig_001"

    @pytest.mark.asyncio
    async def test_matrix_fallback_text_reply_omits_m_relates_to(self, matrix_renderer):
        """Matrix fallback_text reply omits m.relates_to and preserves body."""
        event = make_reply_event(
            source_adapter=MATRIX_ADAPTER_ID,
            target_adapter=MATRIX_ADAPTER_ID,
            target_message_id="$orig_001",
            body="Fallback reply",
        )
        ctx = RenderingContext(
            delivery_strategy="fallback_text",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        assert "Fallback reply" in result.payload.get("body", "")


# ---------------------------------------------------------------------------
# Meshtastic rendering conformance
# ---------------------------------------------------------------------------


class TestMeshtasticRenderingConformance:
    """Assert Meshtastic renderer output matches MEDRE rendering contracts."""

    @pytest.mark.asyncio
    async def test_meshtastic_direct_text_includes_text_channel_meshnet(
        self, meshtastic_renderer
    ):
        """Meshtastic direct text includes text, channel_index."""
        event = make_text_event(source_adapter=MESHTASTIC_ADAPTER_ID, body="Hello mesh")
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MESHTASTIC_ADAPTER_ID,
            target_platform="meshtastic",
        )
        result = await meshtastic_renderer.render(event, ctx)
        assert set(result.payload.keys()) == {"text", "channel_index"}
        assert "meshnet_name" not in result.payload

    @pytest.mark.asyncio
    async def test_meshtastic_fallback_text_reply_preserves_channel(
        self, meshtastic_renderer
    ):
        """Meshtastic fallback_text reply preserves channel_index."""
        event = make_reply_event(
            source_adapter=MESHTASTIC_ADAPTER_ID,
            target_adapter=MESHTASTIC_ADAPTER_ID,
            target_channel="0",
            target_message_id="42",
            body="Fallback reply",
        )
        ctx = RenderingContext(
            delivery_strategy="fallback_text",
            target_adapter=MESHTASTIC_ADAPTER_ID,
            target_platform="meshtastic",
        )
        result = await meshtastic_renderer.render(event, ctx)
        assert result.payload.get("channel_index") == 0
        assert result.fallback_applied == "strategy_fallback_text"
        assert "Fallback reply" in result.payload.get("text", "")

    @pytest.mark.asyncio
    async def test_meshtastic_byte_budget_truncation_evidence(
        self, meshtastic_renderer
    ):
        """Meshtastic byte-budget truncation reports correct limits."""
        # Create text that exceeds 227-byte budget
        long_body = "A" * 300
        event = make_text_event(
            source_adapter=MESHTASTIC_ADAPTER_ID,
            body=long_body,
        )
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MESHTASTIC_ADAPTER_ID,
            target_platform="meshtastic",
        )
        result = await meshtastic_renderer.render(event, ctx)
        assert result.truncated is True
        rendered_text = result.payload.get("text", "")
        assert len(rendered_text.encode("utf-8")) <= 227
        assert result.metadata.get("max_text_bytes") == 227
        assert result.metadata.get("truncated") is True
        assert isinstance(result.metadata.get("original_text_bytes"), int)
        assert isinstance(result.metadata.get("rendered_text_bytes"), int)


# ---------------------------------------------------------------------------
# RenderingEvidence conformance
# ---------------------------------------------------------------------------


class TestRenderingEvidenceConformance:
    """Assert RenderingEvidence captures correct decision inputs."""

    def test_evidence_schema_version_is_one(self):
        """Evidence schema_version is currently '1'."""
        assert EVIDENCE_SCHEMA_VERSION == "1"

    @pytest.mark.asyncio
    async def test_evidence_from_pipeline_render(self, matrix_renderer):
        """Pipeline.render attaches RenderingEvidence with correct fields."""
        pipeline = RenderingPipeline()
        pipeline.register(matrix_renderer, priority=10)
        pipeline.register_adapter_platform(MATRIX_ADAPTER_ID, "matrix")

        event = make_text_event(source_adapter="matrix_conf", body="Evidence test")
        result = await pipeline.render(
            event,
            target_adapter=MATRIX_ADAPTER_ID,
            delivery_strategy="direct",
        )

        evidence = result.rendering_evidence
        assert evidence is not None
        assert evidence.schema_version == "1"
        assert evidence.renderer == "matrix"
        assert evidence.target_adapter == MATRIX_ADAPTER_ID
        assert evidence.target_platform == "matrix"
        assert evidence.delivery_strategy == "direct"
        assert isinstance(evidence.rendered_text_chars, int)
        assert isinstance(evidence.rendered_text_bytes, int)
        assert evidence.truncated is False

    @pytest.mark.asyncio
    async def test_evidence_includes_capability_level(self, meshtastic_renderer):
        """Evidence records capability_level from rendering context."""
        pipeline = RenderingPipeline()
        pipeline.register(meshtastic_renderer, priority=10)
        pipeline.register_adapter_platform(MESHTASTIC_ADAPTER_ID, "meshtastic")

        event = make_text_event(source_adapter="mesh_conf", body="Cap test")
        result = await pipeline.render(
            event,
            target_adapter=MESHTASTIC_ADAPTER_ID,
            delivery_strategy="direct",
            capability_level="native",
        )

        evidence = result.rendering_evidence
        assert evidence is not None
        assert evidence.capability_level == "native"

    @pytest.mark.asyncio
    async def test_evidence_fallback_strategy_recorded(self, matrix_renderer):
        """Evidence records fallback_applied when fallback_text is used."""
        event = make_reply_event(
            source_adapter=MATRIX_ADAPTER_ID,
            target_adapter=MATRIX_ADAPTER_ID,
            target_message_id="$orig",
            body="Fallback test",
        )
        ctx = RenderingContext(
            delivery_strategy="fallback_text",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        assert evidence.fallback_applied == "strategy_fallback_text"
        assert evidence.delivery_strategy == "fallback_text"

    @pytest.mark.asyncio
    async def test_evidence_to_dict_keys_stable(self, matrix_renderer):
        """Evidence.to_dict() produces a stable set of keys."""
        event = make_text_event(body="Dict test")
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        expected_keys = {
            "schema_version",
            "renderer",
            "delivery_strategy",
            "target_adapter",
            "target_platform",
            "target_channel",
            "max_text_chars",
            "max_text_bytes",
            "capability_level",
            "capability_policy",
            "fallback_applied",
            "truncated",
            "rendered_text_chars",
            "rendered_text_bytes",
            "original_text_chars",
            "original_text_bytes",
            "conversation_id",
            "root_event_id",
            "relation_evidence",
        }
        assert set(d.keys()) == expected_keys

    @pytest.mark.asyncio
    async def test_evidence_serializes_to_parseable_json(self, matrix_renderer):
        """Evidence.to_dict() serializes via json.dumps to a parseable blob.

        Verifies that the full evidence dict round-trips through JSON
        serialization and includes the mandatory keys: schema_version,
        renderer, delivery_strategy, target_adapter, capability_level,
        and text char/byte metrics.
        """
        event = make_text_event(body="JSON round-trip test")
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter=MATRIX_ADAPTER_ID,
            target_platform="matrix",
        )
        result = await matrix_renderer.render(event, ctx)
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="matrix",
            ctx=ctx,
            result=result,
        )
        d = evidence.to_dict()
        blob = json.dumps(d, sort_keys=True)
        parsed = json.loads(blob)

        mandatory_keys = {
            "schema_version",
            "renderer",
            "delivery_strategy",
            "target_adapter",
            "capability_level",
            "rendered_text_chars",
            "rendered_text_bytes",
        }
        for key in mandatory_keys:
            assert key in parsed, f"Missing mandatory key: {key!r}"

        assert parsed["schema_version"] == "1"
        assert parsed["renderer"] == "matrix"
        assert parsed["delivery_strategy"] == "direct"
        assert parsed["target_adapter"] == MATRIX_ADAPTER_ID
        assert isinstance(parsed["rendered_text_chars"], int)
        assert isinstance(parsed["rendered_text_bytes"], int)
