"""RenderingResult immutability and metadata serialization tests.

Covers:
- RenderingResult is a frozen dataclass — fields cannot be mutated.
- RenderingResult metadata is JSON-serializable without re-parsing.
- Core RenderingResult fields (excluding payload) are JSON-safe.
"""

from __future__ import annotations

import json

import pytest

from medre.core.rendering.renderer import RenderingResult
from tests.helpers.rendering_evidence import (
    make_context,
    make_event,
    make_lxmf_renderer,
    make_matrix_renderer,
    make_meshcore_renderer,
    make_meshtastic_renderer,
)

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
        renderer = make_meshtastic_renderer()
        event = make_event()
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "meshtastic"

    async def test_meshcore_metadata_json_serializable(self) -> None:
        renderer = make_meshcore_renderer()
        event = make_event()
        ctx = make_context(
            target_adapter="mc-target",
            target_platform="meshcore",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "meshcore"

    async def test_lxmf_metadata_json_serializable(self) -> None:
        renderer = make_lxmf_renderer()
        event = make_event()
        ctx = make_context(
            target_adapter="lxmf-target",
            target_platform="lxmf",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "lxmf"

    async def test_matrix_metadata_json_serializable(self) -> None:
        renderer = make_matrix_renderer()
        event = make_event()
        ctx = make_context(
            target_adapter="matrix-target",
            target_platform="matrix",
        )
        result = await renderer.render(event, ctx)
        serialized = json.dumps(result.metadata)
        parsed = json.loads(serialized)
        assert parsed["renderer"] == "matrix"

    async def test_truncation_metadata_json_serializable(self) -> None:
        """Byte truncation metadata (int values) serializes cleanly."""
        renderer = make_meshtastic_renderer(max_text_bytes=10)
        event = make_event(payload={"text": "A" * 100})
        ctx = make_context(
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
        renderer = make_meshtastic_renderer()
        event = make_event()
        ctx = make_context(
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
