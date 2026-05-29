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

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingResult,
)

# ---------------------------------------------------------------------------
# Gated imports — RenderingEvidence / rendering_evidence may not exist yet.
# ---------------------------------------------------------------------------

try:
    from medre.core.rendering.evidence import RenderingEvidence  # type: ignore[import-not-found]

    HAS_RENDERING_EVIDENCE = True
except ImportError:
    RenderingEvidence = None  # type: ignore[assignment,misc]
    HAS_RENDERING_EVIDENCE = False

try:
    from medre.core.events.canonical import DeliveryReceipt

    HAS_DELIVERY_RECEIPT = True
except ImportError:
    DeliveryReceipt = None  # type: ignore[assignment,misc]
    HAS_DELIVERY_RECEIPT = False

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


@pytest.mark.skipif(
    not HAS_RENDERING_EVIDENCE,
    reason="RenderingEvidence not yet implemented (parallel agent pending)",
)
class TestRenderingEvidenceModel:
    """Tests for the RenderingEvidence model itself.

    These tests are gated on the RenderingEvidence class being available.
    They will be skipped until the parallel evidence-model agent lands.
    """

    def test_evidence_captures_context_fields(self) -> None:
        """RenderingEvidence records key RenderingContext fields."""
        assert RenderingEvidence is not None  # for type checker
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
        evidence = RenderingEvidence.from_context_and_result(  # type: ignore[union-attr]
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.target_adapter == "adapter-1"
        assert evidence.target_platform == "meshtastic"
        assert evidence.delivery_strategy == "direct"

    def test_evidence_captures_truncation_metadata(self) -> None:
        """RenderingEvidence records truncation from RenderingResult metadata."""
        assert RenderingEvidence is not None
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
        evidence = RenderingEvidence.from_context_and_result(  # type: ignore[union-attr]
            renderer_name="meshtastic",
            ctx=ctx,
            result=result,
        )
        assert evidence.truncated is True
        assert evidence.original_text_chars == 100
        assert evidence.rendered_text_bytes is not None

    def test_evidence_records_fallback_applied(self) -> None:
        """RenderingEvidence records fallback_applied when present."""
        assert RenderingEvidence is not None
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
        evidence = RenderingEvidence.from_context_and_result(  # type: ignore[union-attr]
            renderer_name="lxmf",
            ctx=ctx,
            result=result,
        )
        assert evidence.fallback_applied == "strategy_fallback_text"

    def test_evidence_immutable(self) -> None:
        """RenderingEvidence is frozen/immutable."""
        assert RenderingEvidence is not None
        evidence = RenderingEvidence(  # type: ignore[union-attr]
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
        )
        with pytest.raises(AttributeError):
            evidence.target_adapter = "changed"  # type: ignore[misc]

    def test_evidence_serializable_to_dict(self) -> None:
        """RenderingEvidence serializes to a JSON-safe dict."""
        assert RenderingEvidence is not None
        evidence = RenderingEvidence(  # type: ignore[union-attr]
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
        )
        d = evidence.to_dict()  # type: ignore[union-attr]
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["target_adapter"] == "adapter-1"

    def test_evidence_serializable_without_payload(self) -> None:
        """Evidence dict does not require payload re-parsing."""
        assert RenderingEvidence is not None
        evidence = RenderingEvidence(  # type: ignore[union-attr]
            schema_version="1",
            renderer="test",
            target_adapter="a",
            target_platform="m",
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
        )
        d = evidence.to_dict()  # type: ignore[union-attr]
        # Must not contain payload or require it for serialization
        assert "payload" not in d
        json.dumps(d)  # must not raise

    def test_to_dict_includes_all_fields_even_none(self) -> None:
        """to_dict() always includes every field, including those that are
        None, for a stable deterministic serialisation shape."""
        assert RenderingEvidence is not None
        evidence = RenderingEvidence(  # type: ignore[union-attr]
            schema_version="1",
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
            rendered_text_chars=None,
            rendered_text_bytes=None,
            original_text_chars=None,
        )
        d = evidence.to_dict()  # type: ignore[union-attr]

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
# DeliveryReceipt rendering_evidence (gated on implementation)
# ===================================================================


@pytest.mark.skipif(
    not HAS_DELIVERY_RECEIPT,
    reason="DeliveryReceipt not importable",
)
class TestDeliveryReceiptRenderingEvidence:
    """Delivery receipt exposes rendering_evidence where integrated.

    These tests verify the seam between rendering evidence and delivery
    receipts.  They are gated on the implementation landing.
    """

    def test_receipt_has_rendering_evidence_field(self) -> None:
        """DeliveryReceipt has a rendering_evidence attribute."""
        assert DeliveryReceipt is not None
        receipt = DeliveryReceipt(
            sequence=1,
            receipt_id="rcpt-1",
            event_id="evt-1",
            target_adapter="adapter-1",
        )
        # Field should exist (may be None initially)
        assert hasattr(receipt, "rendering_evidence")

    @pytest.mark.skipif(
        not HAS_RENDERING_EVIDENCE,
        reason="RenderingEvidence not yet implemented",
    )
    def test_receipt_rendering_evidence_serializable(self) -> None:
        """DeliveryReceipt.rendering_evidence is JSON-serializable when
        populated with a RenderingEvidence snapshot."""
        assert DeliveryReceipt is not None
        assert RenderingEvidence is not None
        evidence = RenderingEvidence(  # type: ignore[union-attr]
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
        )
        evidence_json = json.dumps(evidence.to_dict())  # type: ignore[union-attr]
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
