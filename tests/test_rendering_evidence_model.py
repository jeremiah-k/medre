"""RenderingEvidence model, delivery receipt, text metrics normalization,
evidence attachment boundary, serialization hardening, and capability-level
tests.

Covers:
- RenderingEvidence model construction, immutability, and serialization.
- DeliveryReceipt rendering_evidence integration.
- Text metrics normalization across payload key variants.
- Evidence attachment boundary (pipeline vs direct vs manual).
- Evidence serialization hardening for edge cases.
- Pipeline capability-level rendering evidence.
- RenderingContext capability_level validation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.renderer import MeshtasticRenderer
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
        from medre.core.rendering.renderer import RenderingPipeline
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
