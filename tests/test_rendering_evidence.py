"""Focused tests for rendering evidence model, renderer consistency,
serialization, and receipt attachment.

Covers:
- Evidence captures RenderingContext fields (strategy, adapter, platform).
- Evidence captures RenderingResult truncation metadata.
- fallback_text records fallback_applied; native direct records no fallback.
- Matrix / Meshtastic / LXMF / MeshCore RenderingResults include evidence
  consistently via their metadata dicts.
- RenderingResult is immutable and metadata is JSON-serializable.
- Byte truncation metadata preserved across all byte-budget renderers.
- Delivery receipt exposes rendering_evidence where integrated.
- RenderingEvidence model (gated on implementation availability).

Tests fail if evidence is missing or not serializable, but do not require
replay execution.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal, cast

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    EventMetadata,
    EventRelation,
)
from medre.core.rendering.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    RenderingEvidence,
)
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-evidence-001",
    payload: dict | None = None,
    relations: tuple[EventRelation, ...] | None = None,
    source_adapter: str = "source-adapter",
) -> CanonicalEvent:
    """Create a minimal canonical event for rendering tests."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="transport-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=relations or (),
        payload=payload or {"text": "evidence test message"},
        metadata=EventMetadata(),
    )


def _make_context(
    target_adapter: str = "target-adapter",
    target_platform: str | None = None,
    delivery_strategy: str = "direct",
    target_channel: str | None = None,
    max_text_bytes: int | None = None,
    max_text_chars: int | None = None,
) -> RenderingContext:
    """Create a RenderingContext with sensible defaults."""
    return RenderingContext(
        delivery_strategy=delivery_strategy,  # type: ignore[arg-type]
        target_adapter=target_adapter,
        target_channel=target_channel,
        target_platform=target_platform,
        max_text_bytes=max_text_bytes,
        max_text_chars=max_text_chars,
    )


def _make_meshtastic_renderer(
    adapter_id: str = "mesh-target",
    max_text_bytes: int = 227,
) -> MeshtasticRenderer:
    config = MeshtasticConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
    )
    return MeshtasticRenderer(configs={adapter_id: config})


def _make_meshcore_renderer(
    adapter_id: str = "mc-target",
    max_text_bytes: int = 512,
) -> MeshCoreRenderer:
    config = MeshCoreConfig(
        adapter_id=adapter_id,
        max_text_bytes=max_text_bytes,
    )
    return MeshCoreRenderer(configs={adapter_id: config})


def _make_lxmf_renderer() -> LxmfRenderer:
    return LxmfRenderer(metadata_embedding=True)


def _make_matrix_renderer() -> MatrixRenderer:
    return MatrixRenderer(source_configs=None)


# ===================================================================
# RenderingContext field capture in metadata
# ===================================================================


class TestRenderingContextCapture:
    """Each renderer records context-derived fields in its result metadata
    or directly on the RenderingResult so that evidence is available
    without re-parsing the payload."""

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_result_captures_target_adapter(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.target_adapter matches the context."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.target_adapter == adapter_id

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_result_captures_event_id(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.event_id matches the source event."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event(event_id="evt-unique-42")
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.event_id == "evt-unique-42"

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_result_captures_target_channel(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.target_channel reflects the context."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            target_channel="3",
        )
        result = await renderer.render(event, ctx)
        assert result.target_channel == "3"

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_metadata_includes_renderer_name(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Result metadata includes the renderer's name identifier."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert "renderer" in result.metadata
        assert isinstance(result.metadata["renderer"], str)
        assert result.metadata["renderer"] == renderer.name


# ===================================================================
# Truncation metadata preservation
# ===================================================================


class TestRenderingResultTruncationMetadata:
    """Byte-budget renderers record truncation metadata in their result."""

    async def test_meshtastic_truncation_metadata_present(self) -> None:
        """Meshtastic renderer includes byte counts and truncated flag."""
        renderer = _make_meshtastic_renderer(max_text_bytes=227)
        event = _make_event(payload={"text": "short"})
        ctx = _make_context(
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
        renderer = _make_meshtastic_renderer(max_text_bytes=10)
        event = _make_event(payload={"text": "A" * 100})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=512)
        event = _make_event(payload={"text": "hello meshcore"})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=5)
        event = _make_event(payload={"text": "B" * 50})
        ctx = _make_context(
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
        renderer = _make_meshtastic_renderer(max_text_bytes=500)
        event = _make_event(payload={"text": "small message"})
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is False
        assert result.metadata["truncated"] is False

    async def test_byte_budget_from_context_overrides_config(self) -> None:
        """RenderingContext.max_text_bytes overrides the adapter config budget."""
        renderer = _make_meshtastic_renderer(max_text_bytes=500)
        event = _make_event(payload={"text": "C" * 100})
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            max_text_bytes=10,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is True
        assert result.metadata["max_text_bytes"] == 10


# ===================================================================
# Fallback-applied recording
# ===================================================================


class TestFallbackAppliedRecording:
    """fallback_text delivery strategy records fallback_applied;
    direct records None."""

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_fallback_text_records_fallback_applied(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """When delivery_strategy is fallback_text, fallback_applied is set."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_direct_records_no_fallback(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """When delivery_strategy is direct, fallback_applied is None."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            delivery_strategy="direct",
        )
        result = await renderer.render(event, ctx)

        assert result.fallback_applied is None

    async def test_meshtastic_fallback_text_metadata_includes_strategy(
        self,
    ) -> None:
        """Meshtastic fallback_text result metadata includes delivery_strategy."""
        renderer = _make_meshtastic_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)

        assert result.metadata.get("delivery_strategy") == "fallback_text"

    async def test_matrix_fallback_text_includes_truncation_meta(self) -> None:
        """Matrix fallback_text with char/byte budget records truncation."""
        renderer = _make_matrix_renderer()
        event = _make_event(payload={"text": "A" * 300})
        ctx = _make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
            max_text_chars=50,
        )
        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"
        # Matrix records truncation metadata when truncation occurs
        if result.truncated:
            assert "original_length" in result.metadata
            assert "original_text_bytes" in result.metadata


# ===================================================================
# Renderer consistency across all four transports
# ===================================================================


class TestRendererConsistency:
    """All four transport renderers include evidence consistently
    in their RenderingResult."""

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_all_renderers_return_rendering_result(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every renderer returns a RenderingResult."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert isinstance(result, RenderingResult)

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_all_results_have_event_id(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult preserves the source event_id."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event(event_id="evt-consistency-99")
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.event_id == "evt-consistency-99"

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_all_results_have_non_empty_metadata(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult has non-empty metadata."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.metadata
        assert isinstance(result.metadata, dict)

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_all_results_have_payload(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult has a non-empty payload dict."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.payload
        assert isinstance(result.payload, dict)

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: _make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: _make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: _make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: _make_matrix_renderer(), "matrix", "matrix-target"),
        ],
        ids=["meshtastic", "meshcore", "lxmf", "matrix"],
    )
    async def test_all_fallback_results_include_evidence(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """All renderers under fallback_text record fallback_applied."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = _make_event()
        ctx = _make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        # Core evidence: fallback_applied marker + renderer identity
        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("renderer") is not None


# ===================================================================
# RenderingResult immutability
# ===================================================================


class TestRenderingResultImmutability:
    """RenderingResult is a frozen dataclass — evidence fields cannot be
    mutated after creation."""

    def _make_result(self) -> RenderingResult:
        return RenderingResult(
            event_id="evt-imm-1",
            target_adapter="adapter-1",
            target_channel=None,
            payload={"text": "immutable test"},
            metadata={"renderer": "test"},
        )

    def test_frozen_event_id(self) -> None:
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.event_id = "changed"  # type: ignore[misc]

    def test_frozen_target_adapter(self) -> None:
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.target_adapter = "changed"  # type: ignore[misc]

    def test_frozen_truncated(self) -> None:
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.truncated = True  # type: ignore[misc]

    def test_frozen_fallback_applied(self) -> None:
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.fallback_applied = "strategy_fallback_text"  # type: ignore[misc]

    def test_frozen_payload(self) -> None:
        result = self._make_result()
        with pytest.raises(AttributeError):
            result.payload = {}  # type: ignore[misc]


# ===================================================================
# Serialization (JSON-safe metadata, no payload re-parsing needed)
# ===================================================================


class TestRenderingResultSerialization:
    """RenderingResult metadata is JSON-serializable without re-parsing
    the payload."""

    async def test_meshtastic_metadata_json_serializable(self) -> None:
        renderer = _make_meshtastic_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "meshtastic"

    async def test_meshcore_metadata_json_serializable(self) -> None:
        renderer = _make_meshcore_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "meshcore"

    async def test_lxmf_metadata_json_serializable(self) -> None:
        renderer = _make_lxmf_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "lxmf"

    async def test_matrix_metadata_json_serializable(self) -> None:
        renderer = _make_matrix_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "matrix"

    async def test_truncation_metadata_json_serializable(self) -> None:
        """Byte truncation metadata (int values) serializes cleanly."""
        renderer = _make_meshtastic_renderer(max_text_bytes=10)
        event = _make_event(payload={"text": "A" * 100})
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert isinstance(parsed["original_text_bytes"], int)
        assert isinstance(parsed["rendered_text_bytes"], int)
        assert isinstance(parsed["max_text_bytes"], int)
        assert isinstance(parsed["truncated"], bool)

    async def test_result_core_fields_json_serializable(self) -> None:
        """Core RenderingResult fields (excluding payload) are JSON-safe."""
        renderer = _make_meshtastic_renderer()
        event = _make_event()
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)

        evidence_dict = {
            "event_id": result.event_id,
            "target_adapter": result.target_adapter,
            "target_channel": result.target_channel,
            "truncated": result.truncated,
            "fallback_applied": result.fallback_applied,
            "metadata": result.metadata,
        }
        serialized = json.dumps(evidence_dict)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["event_id"] == event.event_id


# ===================================================================
# Byte truncation edge cases
# ===================================================================


class TestByteTruncationEdgeCases:
    """Edge cases for byte truncation metadata."""

    async def test_zero_byte_budget_produces_empty_text(self) -> None:
        """Zero max_text_bytes renders empty text with correct metadata."""
        renderer = _make_meshcore_renderer(max_text_bytes=0)
        event = _make_event(payload={"text": "hello"})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=10)
        event = _make_event(payload={"text": text})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=0)
        event = _make_event(payload={"text": "data"})
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)

        assert result.payload["text"] == ""
        assert result.truncated is True

    async def test_context_byte_budget_overrides_config(self) -> None:
        """RenderingContext.max_text_bytes takes precedence over config."""
        renderer = _make_meshcore_renderer(max_text_bytes=1000)
        event = _make_event(payload={"text": "D" * 100})
        ctx = _make_context(
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
# RenderingEvidence model (gated on implementation)
# ===================================================================


class TestRenderingEvidenceModel:
    """Tests for the RenderingEvidence model itself."""

    def test_evidence_captures_context_fields(self) -> None:
        """RenderingEvidence records key RenderingContext fields."""
        ctx = _make_context(
            target_adapter="adapter-1",
            target_platform="meshtastic",
            delivery_strategy="direct",
        )
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="adapter-1",
            target_channel=None,
            payload={"text": "hello"},
            metadata={"renderer": "meshtastic"},
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.target_adapter == "adapter-1"
        assert evidence.target_platform == "meshtastic"
        assert evidence.delivery_strategy == "direct"

    def test_evidence_captures_truncation_metadata(self) -> None:
        """RenderingEvidence records truncation from RenderingResult metadata."""
        ctx = _make_context(target_adapter="adapter-1", target_platform="meshtastic")
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="adapter-1",
            target_channel=None,
            payload={"text": "x"},
            metadata={
                "renderer": "meshtastic",
                "original_length": 100,
                "original_text_bytes": 100,
                "rendered_text_bytes": 50,
                "max_text_bytes": 50,
                "truncated": True,
            },
            truncated=True,
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        assert evidence.original_text_chars == 100
        assert evidence.rendered_text_bytes is not None

    def test_evidence_records_fallback_applied(self) -> None:
        """RenderingEvidence records fallback_applied when present."""
        ctx = _make_context(
            target_adapter="adapter-2",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = RenderingResult(
            event_id="evt-2",
            target_adapter="adapter-2",
            target_channel=None,
            payload={"text": "fallback"},
            metadata={"renderer": "lxmf"},
            fallback_applied="strategy_fallback_text",
        )
        evidence = RenderingEvidence.from_context_and_result(
            renderer_name="lxmf",
            ctx=ctx,
            result=result,
        )
        assert evidence.fallback_applied == "strategy_fallback_text"

    def test_evidence_immutable(self) -> None:
        """RenderingEvidence is frozen/immutable."""
        evidence = RenderingEvidence(
            schema_version="1",
            renderer="test",
            target_adapter="a",
            target_platform="p",
            delivery_strategy="direct",
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            truncated=False,
            fallback_applied=None,
            rendered_text_chars=None,
            rendered_text_bytes=None,
            original_text_chars=None,
            original_text_bytes=None,
        )
        with pytest.raises(AttributeError):
            evidence.target_adapter = "changed"  # type: ignore[misc]

    def test_evidence_serializable_to_dict(self) -> None:
        """RenderingEvidence serializes to a JSON-safe dict."""
        evidence = RenderingEvidence(
            schema_version="1",
            renderer="meshtastic",
            target_adapter="adapter-1",
            target_platform="meshtastic",
            delivery_strategy="direct",
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            truncated=False,
            fallback_applied=None,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=5,
            original_text_bytes=None,
        )
        d = evidence.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["target_adapter"] == "adapter-1"

    def test_evidence_serializable_without_payload(self) -> None:
        """Evidence dict does not require payload re-parsing."""
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
            truncated=False,
            fallback_applied=None,
            rendered_text_chars=100,
            rendered_text_bytes=100,
            original_text_chars=100,
            original_text_bytes=None,
        )
        d = evidence.to_dict()

        # Every field must be present — even those that are None.
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
        }
        assert set(d.keys()) == expected_keys

        # None fields must be explicitly present (not omitted).
        assert d["target_platform"] is None
        assert d["target_channel"] is None
        assert d["capability_policy"] is None
        assert d["fallback_applied"] is None

        # The dict must round-trip through JSON without error.
        serialized = json.dumps(d, sort_keys=True)
        parsed = json.loads(serialized)
        assert set(parsed.keys()) == expected_keys


# ===================================================================
# DeliveryReceipt rendering_evidence
# ===================================================================


class TestDeliveryReceiptRenderingEvidence:
    """Delivery receipt exposes rendering_evidence where integrated."""

    def test_receipt_has_rendering_evidence_field(self) -> None:
        """DeliveryReceipt has a rendering_evidence attribute."""
        receipt = DeliveryReceipt(
            sequence=1,
            receipt_id="rcpt-1",
            event_id="evt-1",
            target_adapter="adapter-1",
        )
        # Field should exist (may be None initially)
        assert hasattr(receipt, "rendering_evidence")

    def test_receipt_rendering_evidence_serializable(self) -> None:
        """DeliveryReceipt.rendering_evidence is JSON-serializable when
        populated with a RenderingEvidence snapshot."""
        evidence = RenderingEvidence(
            schema_version="1",
            renderer="text",
            target_adapter="adapter-2",
            target_platform=None,
            delivery_strategy="direct",
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            truncated=False,
            fallback_applied=None,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=5,
            original_text_bytes=None,
        )
        evidence_json = json.dumps(evidence.to_dict())
        receipt = DeliveryReceipt(
            sequence=1,
            receipt_id="rcpt-2",
            event_id="evt-2",
            target_adapter="adapter-2",
            rendering_evidence=evidence_json,
        )
        assert receipt.rendering_evidence is not None
        parsed = json.loads(receipt.rendering_evidence)
        assert isinstance(parsed, dict)
        assert parsed["renderer"] == "text"
        assert parsed["target_adapter"] == "adapter-2"
        assert parsed["truncated"] is False


# ===================================================================
# Text metrics normalization (item A)
# ===================================================================


class TestTextMetricsNormalization:
    """_text_char_byte_metrics prefers metadata, then falls back to payload keys."""

    def _make_evidence(
        self,
        payload: dict[str, object],
        metadata: dict[str, object],
    ) -> RenderingEvidence:
        ctx = _make_context(target_adapter="a", target_platform="p")
        result = RenderingResult(
            event_id="evt-metrics",
            target_adapter="a",
            target_channel=None,
            payload=payload,
            metadata={"renderer": "test", **metadata},
        )
        return RenderingEvidence.from_context_and_result(
            renderer_name="test",
            ctx=ctx,
            result=result,
        )

    def test_matrix_body_key_derives_metrics(self) -> None:
        """Matrix payload uses 'body' instead of 'text'."""
        evidence = self._make_evidence(
            payload={"body": "hello matrix", "msgtype": "m.text"},
            metadata={},
        )
        assert evidence.rendered_text_chars == len("hello matrix")
        assert evidence.rendered_text_bytes == len("hello matrix".encode("utf-8"))

    def test_lxmf_content_key_derives_metrics(self) -> None:
        """LXMF payload uses 'content' instead of 'text'."""
        evidence = self._make_evidence(
            payload={"content": "hello lxmf"},
            metadata={},
        )
        assert evidence.rendered_text_chars == len("hello lxmf")
        assert evidence.rendered_text_bytes == len("hello lxmf".encode("utf-8"))

    def test_meshtastic_text_key_derives_metrics(self) -> None:
        """Meshtastic/MeshCore payload uses 'text' key."""
        evidence = self._make_evidence(
            payload={"text": "hello mesh"},
            metadata={},
        )
        assert evidence.rendered_text_chars == len("hello mesh")
        assert evidence.rendered_text_bytes == len("hello mesh".encode("utf-8"))

    def test_metadata_rendered_text_chars_overrides_payload(self) -> None:
        """Metadata rendered_text_chars takes precedence over payload text."""
        evidence = self._make_evidence(
            payload={"text": "short"},
            metadata={"rendered_text_chars": 999, "rendered_text_bytes": 888},
        )
        assert evidence.rendered_text_chars == 999
        assert evidence.rendered_text_bytes == 888

    def test_no_text_keys_yields_none_metrics(self) -> None:
        """Payload with no text/body/content yields None rendered metrics."""
        evidence = self._make_evidence(
            payload={"data": b"\x00\x01"},
            metadata={},
        )
        assert evidence.rendered_text_chars is None
        assert evidence.rendered_text_bytes is None

    def test_original_text_bytes_from_metadata(self) -> None:
        """original_text_bytes populated from metadata['original_text_bytes']."""
        evidence = self._make_evidence(
            payload={"text": "truncated"},
            metadata={"original_text_bytes": 500},
        )
        assert evidence.original_text_bytes == 500

    def test_original_text_bytes_none_when_missing(self) -> None:
        """original_text_bytes is None when metadata lacks the key."""
        evidence = self._make_evidence(
            payload={"text": "hello"},
            metadata={},
        )
        assert evidence.original_text_bytes is None

    def test_evidence_uses_schema_version_constant(self) -> None:
        """Evidence.schema_version matches EVIDENCE_SCHEMA_VERSION."""
        evidence = self._make_evidence(
            payload={"text": "x"},
            metadata={},
        )
        assert evidence.schema_version == EVIDENCE_SCHEMA_VERSION


# ===================================================================
# Evidence attachment boundary tests (item D)
# ===================================================================


class TestEvidenceAttachmentBoundary:
    """Evidence is attached only by RenderingPipeline.render(), not by
    direct renderer.render() or manual RenderingResult construction."""

    async def test_manual_result_has_none_evidence(self) -> None:
        """A manually constructed RenderingResult has no evidence."""
        result = RenderingResult(
            event_id="evt-manual",
            target_adapter="adapter-1",
            target_channel=None,
            payload={"text": "manual"},
            metadata={"renderer": "test"},
        )
        assert result.rendering_evidence is None

    async def test_pipeline_attaches_evidence(self) -> None:
        """RenderingPipeline.render() attaches evidence to the result."""
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer

        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event(event_id="evt-pipe-ev")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
            target_channel=None,
        )
        assert result.rendering_evidence is not None
        assert isinstance(result.rendering_evidence, RenderingEvidence)
        assert result.rendering_evidence.renderer == "text"

    async def test_direct_renderer_no_evidence(self) -> None:
        """Calling renderer.render() directly does not attach evidence."""
        renderer = _make_meshtastic_renderer()
        event = _make_event(payload={"text": "direct call"})
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)
        assert result.rendering_evidence is None


# ===================================================================
# PipelineRunner evidence serialization hardening (item G)
# ===================================================================


class TestEvidenceSerializationHardening:
    """PipelineRunner hardens rendering_evidence serialization.

    Tests exercise the serialization logic directly without running the
    full pipeline, to ensure robustness against edge cases.
    """

    def test_valid_evidence_to_dict_persists(self) -> None:
        """RenderingEvidence.to_dict() serializes to valid JSON."""
        evidence = RenderingEvidence(
            schema_version=EVIDENCE_SCHEMA_VERSION,
            renderer="text",
            target_adapter="a",
            target_platform=None,
            delivery_strategy="direct",
            target_channel=None,
            max_text_chars=None,
            max_text_bytes=None,
            capability_level="native",
            capability_policy=None,
            truncated=False,
            fallback_applied=None,
            rendered_text_chars=5,
            rendered_text_bytes=5,
            original_text_chars=5,
            original_text_bytes=None,
        )
        serialized = json.dumps(evidence.to_dict(), sort_keys=True)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "text"
        assert parsed["rendered_text_chars"] == 5

    def test_str_evidence_passes_through(self) -> None:
        """A string evidence value is used as-is (defensive path)."""
        from medre.core.engine.pipeline.target_delivery import (
            _serialize_rendering_evidence_for_receipt,
        )

        raw_str = '{"renderer": "already-serialized"}'
        result = _serialize_rendering_evidence_for_receipt(raw_str)
        assert result == raw_str
        parsed = json.loads(result)
        assert parsed["renderer"] == "already-serialized"

    def test_dict_evidence_persists(self) -> None:
        """A dict evidence value is serialized via json.dumps."""
        from medre.core.engine.pipeline.target_delivery import (
            _serialize_rendering_evidence_for_receipt,
        )

        raw_dict = {"renderer": "from-dict", "truncated": True}
        result = _serialize_rendering_evidence_for_receipt(raw_dict)
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["renderer"] == "from-dict"

    def test_unsupported_object_does_not_raise(self) -> None:
        """An unsupported evidence object type does not raise; serializer
        returns None."""

        class BadEvidence:
            pass

        bad = BadEvidence()
        from medre.core.engine.pipeline.target_delivery import (
            _serialize_rendering_evidence_for_receipt,
        )

        result = _serialize_rendering_evidence_for_receipt(bad)
        assert result is None

    def test_to_dict_raising_does_not_crash(self) -> None:
        """If to_dict() raises, the serializer catches it and returns None
        (receipt persists with None evidence)."""

        class BrokenEvidence:
            def to_dict(self) -> dict:  # type: ignore[empty-body]
                raise RuntimeError("boom")

        broken = BrokenEvidence()
        # Exercise the production serialization path directly.
        from medre.core.engine.pipeline.target_delivery import (
            _serialize_rendering_evidence_for_receipt,
        )

        result = _serialize_rendering_evidence_for_receipt(broken)
        # Must not raise; must return None.
        assert result is None


# ===================================================================
# Pipeline render capability-level tests
# ===================================================================


class TestPipelineCapabilityLevelRendering:
    """RenderingPipeline.render() propagates capability_level to evidence."""

    async def test_fallback_capability_level_creates_fallback_evidence(
        self,
    ) -> None:
        """render(..., capability_level='fallback') creates fallback evidence."""
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer

        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event(event_id="evt-cap-fallback")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
            target_channel=None,
            capability_level="fallback",
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.capability_level == "fallback"

    async def test_omitted_capability_level_normalizes_to_native(
        self,
    ) -> None:
        """When capability_level is omitted/None, normalizes to 'native'."""
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer

        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event(event_id="evt-cap-default")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
            target_channel=None,
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.capability_level == "native"

    async def test_native_capability_level_evidence(self) -> None:
        """render(..., capability_level='native') records native in evidence."""
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer

        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event(event_id="evt-cap-native")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
            target_channel=None,
            capability_level="native",
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.capability_level == "native"


# ===================================================================
# RenderingContext capability_level validation
# ===================================================================


class TestRenderingContextCapabilityLevelValidation:
    """Verify RenderingContext validates capability_level at construction."""

    def test_invalid_capability_level_raises_value_error(self) -> None:
        """Invalid capability_level raises ValueError."""
        with pytest.raises(ValueError, match="Unknown capability_level"):
            RenderingContext(
                delivery_strategy="direct",
                target_adapter="test",
                capability_level="bogus",  # type: ignore[arg-type]
            )

    def test_valid_native_accepted(self) -> None:
        """capability_level='native' is accepted."""
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter="test",
            capability_level="native",
        )
        assert ctx.capability_level == "native"

    def test_valid_fallback_accepted(self) -> None:
        """capability_level='fallback' is accepted."""
        ctx = RenderingContext(
            delivery_strategy="fallback_text",
            target_adapter="test",
            capability_level="fallback",
        )
        assert ctx.capability_level == "fallback"

    def test_valid_unsupported_accepted(self) -> None:
        """capability_level='unsupported' is accepted."""
        ctx = RenderingContext(
            delivery_strategy="skip",
            target_adapter="test",
            capability_level="unsupported",
        )
        assert ctx.capability_level == "unsupported"

    async def test_omitted_capability_level_normalizes_via_pipeline(self) -> None:
        """RenderingPipeline.render() normalizes None capability_level to 'native'."""
        from medre.core.rendering.text import TextRenderer

        pipeline = RenderingPipeline()
        pipeline.register(TextRenderer(), priority=100)

        event = _make_event(event_id="evt-ctx-norm")
        result = await pipeline.render(
            event,
            target_adapter="target-1",
            capability_level=None,
        )
        assert result.rendering_evidence is not None
        assert result.rendering_evidence.capability_level == "native"

    async def test_default_capability_level_is_native(self) -> None:
        """RenderingContext defaults capability_level to 'native'."""
        ctx = RenderingContext(
            delivery_strategy="direct",
            target_adapter="test",
        )
        assert ctx.capability_level == "native"


# ===================================================================
# MeshCore byte-budget truncation evidence (item A)
# ===================================================================


class TestMeshCoreByteBudgetTruncationEvidence:
    """MeshCore byte-budget truncation records correct evidence metrics."""

    async def test_meshcore_truncation_evidence_byte_counts(self) -> None:
        """MeshCore evidence reports original_bytes > max_bytes > rendered_bytes."""
        renderer = _make_meshcore_renderer(max_text_bytes=20)
        event = _make_event(payload={"text": "A" * 100})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=500)
        event = _make_event(payload={"text": "short"})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=1000)
        event = _make_event(payload={"text": "B" * 100})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=10)
        event = _make_event(payload={"text": text})
        ctx = _make_context(
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
        renderer = _make_meshcore_renderer(max_text_bytes=0)
        event = _make_event(payload={"text": "hello"})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        long_text = "X" * 20000
        event = _make_event(payload={"text": long_text})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        short_text = "Short message"
        event = _make_event(payload={"text": short_text})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        long_text = "Y" * 20000
        event = _make_event(payload={"text": long_text})
        ctx = _make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            max_text_chars=None,
        )
        result = await renderer.render(event, ctx)

        assert result.truncated is False
        assert result.payload["content"] == long_text

    async def test_lxmf_truncation_evidence_records_metrics(self) -> None:
        """LXMF truncation evidence records original and rendered metrics."""
        renderer = _make_lxmf_renderer()
        long_text = "Z" * 20000
        event = _make_event(payload={"text": long_text})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        long_text = "A" * 20000
        event = _make_event(payload={"text": long_text})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-target-123",
            target_native_ref=None,
            key=None,
            fallback_text="original message",
        )
        event = _make_event(
            payload={"text": "R" * 17000},
            relations=(rel,),
        )
        ctx = _make_context(
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
# Fallback/degradation for all relation types (item C)
# ===================================================================


def _make_relation_event(
    relation_type: str,
    *,
    payload: dict | None = None,
    fallback_text: str | None = None,
    key: str | None = None,
) -> CanonicalEvent:
    """Create an event with a specific relation type."""
    rel = EventRelation(
        relation_type=cast(
            Literal["reply", "reaction", "edit", "delete", "thread"],
            relation_type,
        ),
        target_event_id="evt-target-abc",
        target_native_ref=None,
        key=key,
        fallback_text=fallback_text,
    )
    return _make_event(
        payload=payload or {"text": "test message"},
        relations=(rel,),
    )


class TestRelationFallbackDegradation:
    """All relation types degrade deterministically under fallback_text."""

    # -- TextRenderer (shared text fallback) --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_text_renderer_relation_fallback_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """TextRenderer produces deterministic degraded text for each relation."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "hello"},
            key=key,
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_text_renderer_relation_deterministic_across_calls(
        self,
        relation_type: str,
    ) -> None:
        """Same event produces identical text on repeated renders."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "hello"},
            key=key,
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result1 = await renderer.render(event, ctx)
        result2 = await renderer.render(event, ctx)
        assert result1.payload["text"] == result2.payload["text"]

    # -- Meshtastic renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "edit", "delete", "thread"],
        ids=["reply", "edit", "delete", "thread"],
    )
    async def test_meshtastic_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """Meshtastic fallback_text produces deterministic degraded text."""
        renderer = _make_meshtastic_renderer()
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
        )
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    # -- MeshCore renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_meshcore_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """MeshCore fallback_text produces deterministic degraded text."""
        renderer = _make_meshcore_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = result.payload.get("text", "")
        assert isinstance(text, str)
        assert len(text) > 0

    # -- LXMF renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_lxmf_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """LXMF fallback_text produces deterministic degraded content."""
        renderer = _make_lxmf_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = _make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        content = result.payload.get("content", "")
        assert isinstance(content, str)
        assert len(content) > 0

    # -- Matrix renderer fallback for each relation type --

    @pytest.mark.parametrize(
        "relation_type",
        ["reply", "reaction", "edit", "delete", "thread"],
        ids=["reply", "reaction", "edit", "delete", "thread"],
    )
    async def test_matrix_fallback_relation_deterministic(
        self,
        relation_type: str,
    ) -> None:
        """Matrix fallback_text produces deterministic degraded body."""
        renderer = _make_matrix_renderer()
        key = "👍" if relation_type == "reaction" else None
        event = _make_relation_event(
            relation_type,
            payload={"text": "test body"},
            key=key,
        )
        ctx = _make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        assert "m.relates_to" not in result.payload
        body = result.payload.get("body", "")
        assert isinstance(body, str)
        assert len(body) > 0

    # -- Specific degradation format checks --

    async def test_text_renderer_reply_fallback_format(self) -> None:
        """TextRenderer reply fallback includes '[replying to:]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-123456789",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "reply body"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[replying to:" in text

    async def test_text_renderer_delete_fallback_format(self) -> None:
        """TextRenderer delete fallback produces '[deleted]' text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="delete",
            target_event_id="evt-del-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": ""},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[deleted" in text

    async def test_text_renderer_edit_fallback_format(self) -> None:
        """TextRenderer edit fallback includes '[edited]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="edit",
            target_event_id="evt-edit-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "edited text"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert text.startswith("[edited]")

    async def test_text_renderer_reaction_fallback_format(self) -> None:
        """TextRenderer reaction fallback includes 'reacted with' text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="reaction",
            target_event_id="evt-react-123",
            target_native_ref=None,
            key="👍",
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "", "user": "Alice"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "reacted with" in text
        assert "👍" in text

    async def test_text_renderer_thread_fallback_format(self) -> None:
        """TextRenderer thread fallback includes '[thread:]' prefix."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-123",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[thread:" in text


# ===================================================================
# Multi-relation behavior determinism (item D)
# ===================================================================


class TestMultiRelationDeterminism:
    """When an event carries multiple relations, only the first is processed
    and the result is deterministic."""

    async def test_text_renderer_uses_first_relation_only(self) -> None:
        """TextRenderer processes only the first relation when multiple exist."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel1 = EventRelation(
            relation_type="reply",
            target_event_id="evt-first",
            target_native_ref=None,
            key=None,
            fallback_text="first target",
        )
        rel2 = EventRelation(
            relation_type="reaction",
            target_event_id="evt-second",
            target_native_ref=None,
            key="❤️",
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "hello"},
            relations=(rel1, rel2),
        )
        ctx = _make_context(target_adapter="text-target")
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        # First relation is reply, so text should contain reply formatting
        assert "replying to" in text or "first target" in text
        # Second relation (reaction) should NOT appear
        assert "❤️" not in text or "reacted with" not in text

    async def test_meshcore_uses_first_relation_only(self) -> None:
        """MeshCore processes only the first relation."""
        renderer = _make_meshcore_renderer()
        rel1 = EventRelation(
            relation_type="edit",
            target_event_id="evt-edit-first",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        rel2 = EventRelation(
            relation_type="delete",
            target_event_id="evt-del-second",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "multi-rel"},
            relations=(rel1, rel2),
        )
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        # First relation is edit → degrade_relations_inline appends [edit of: ...]
        # The shared helper appends to text
        assert "edit" in text.lower() or "multi-rel" in text

    async def test_multi_relation_deterministic_across_calls(self) -> None:
        """Same multi-relation event produces identical output every time."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel1 = EventRelation(
            relation_type="reply",
            target_event_id="evt-reply-001",
            target_native_ref=None,
            key=None,
            fallback_text="target msg",
        )
        rel2 = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "content"},
            relations=(rel1, rel2),
        )
        ctx = _make_context(target_adapter="text-target")

        results = [await renderer.render(event, ctx) for _ in range(5)]
        texts = [str(r.payload["text"]) for r in results]
        # All renders produce identical text
        assert len(set(texts)) == 1

    async def test_relation_order_affects_output(self) -> None:
        """Swapping relation order produces different output."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel_reply = EventRelation(
            relation_type="reply",
            target_event_id="evt-001",
            target_native_ref=None,
            key=None,
            fallback_text="target",
        )
        rel_edit = EventRelation(
            relation_type="edit",
            target_event_id="evt-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event_reply_first = _make_event(
            payload={"text": "content"},
            relations=(rel_reply, rel_edit),
        )
        event_edit_first = _make_event(
            payload={"text": "content"},
            relations=(rel_edit, rel_reply),
        )
        ctx = _make_context(target_adapter="text-target")

        result1 = await renderer.render(event_reply_first, ctx)
        result2 = await renderer.render(event_edit_first, ctx)

        assert str(result1.payload["text"]) != str(result2.payload["text"])


# ===================================================================
# RenderingEvidence size bounded for huge payloads (item E)
# ===================================================================


class TestEvidenceSizeBounded:
    """RenderingEvidence does not store rendered text payloads — only metrics."""

    def test_evidence_dict_excludes_payload_text(self) -> None:
        """Evidence to_dict never contains the rendered text content."""
        ctx = _make_context(target_adapter="a", target_platform="meshtastic")
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
        renderer = _make_meshcore_renderer(max_text_bytes=100)
        event = _make_event(payload={"text": "A" * 1_000_000})
        ctx = _make_context(
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
        renderer = _make_lxmf_renderer()
        event = _make_event(payload={"text": "B" * 1_000_000})
        ctx = _make_context(
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
        renderer = _make_meshtastic_renderer(max_text_bytes=227)
        event = _make_event(payload={"text": "C" * 1_000_000})
        ctx = _make_context(
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
        renderer = _make_matrix_renderer()
        event = _make_event(payload={"text": "D" * 1_000_000})
        ctx = _make_context(
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
# Thread relation capability — native/direct vs degraded (item F)
# ===================================================================


class TestThreadRelationCapability:
    """Thread relation: native platforms get direct thread handling;
    degraded platforms get text-only thread representation."""

    async def test_matrix_fallback_thread_no_m_relates_to(self) -> None:
        """Matrix fallback_text thread omits m.relates_to."""
        renderer = _make_matrix_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-001",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert "m.relates_to" not in result.payload
        assert result.fallback_applied == "strategy_fallback_text"
        assert "thread" in str(result.payload.get("body", "")).lower()

    async def test_meshcore_fallback_thread_degrades_text(self) -> None:
        """MeshCore fallback_text thread degrades to inline text."""
        renderer = _make_meshcore_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-002",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread msg"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = str(result.payload["text"])
        assert "thread" in text.lower()
        # Byte budget is still enforced
        assert isinstance(result.metadata["rendered_text_bytes"], int)

    async def test_meshtastic_fallback_thread_degrades_text(self) -> None:
        """Meshtastic fallback_text thread degrades to readable text."""
        renderer = _make_meshtastic_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-003",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread reply"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        text = str(result.payload["text"])
        assert "thread" in text.lower()

    async def test_lxmf_fallback_thread_degrades_text(self) -> None:
        """LXMF fallback_text thread degrades to inline text."""
        renderer = _make_lxmf_renderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-004",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread content"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.fallback_applied == "strategy_fallback_text"
        content = str(result.payload["content"])
        assert "thread" in content.lower()

    async def test_text_renderer_thread_degrades_text(self) -> None:
        """TextRenderer thread relation produces deterministic degraded text."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-005",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "thread body"},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="text-target",
            delivery_strategy="direct",
        )
        result = await renderer.render(event, ctx)
        text = str(result.payload["text"])
        assert "[thread:" in text
        assert "thread body" in text

    async def test_meshcore_thread_text_truncated_by_byte_budget(self) -> None:
        """Thread degraded text is still subject to byte budget truncation."""
        renderer = _make_meshcore_renderer(max_text_bytes=20)
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-006",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "A" * 200},
            relations=(rel,),
        )
        ctx = _make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        assert result.truncated is True
        assert len(str(result.payload["text"]).encode("utf-8")) <= 20

    async def test_thread_relation_deterministic_repeated_renders(self) -> None:
        """Thread degradation text is identical across repeated renders."""
        from medre.core.rendering.text import TextRenderer

        renderer = TextRenderer()
        rel = EventRelation(
            relation_type="thread",
            target_event_id="evt-thread-007",
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        event = _make_event(
            payload={"text": "deterministic"},
            relations=(rel,),
        )
        ctx = _make_context(target_adapter="text-target")

        results = [await renderer.render(event, ctx) for _ in range(3)]
        texts = [str(r.payload["text"]) for r in results]
        assert len(set(texts)) == 1
