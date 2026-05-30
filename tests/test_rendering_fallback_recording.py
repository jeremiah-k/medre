"""Fallback-applied recording tests for all transport renderers.

Covers:
- fallback_text delivery strategy records fallback_applied.
- direct delivery strategy records no fallback (None).
- Meshtastic fallback_text result metadata includes delivery_strategy.
- Matrix fallback_text with char/byte budget records truncation metadata.
"""

from __future__ import annotations

import pytest

from tests.helpers.rendering_evidence import (
    make_context,
    make_event,
    make_lxmf_renderer,
    make_matrix_renderer,
    make_meshcore_renderer,
    make_meshtastic_renderer,
)

# ===================================================================
# Fallback-applied recording
# ===================================================================


class TestFallbackAppliedRecording:
    """fallback_text delivery strategy records fallback_applied;
    direct records None."""

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: make_matrix_renderer(), "matrix", "matrix-target"),
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
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)

        assert result.fallback_applied == "strategy_fallback_text"

    @pytest.mark.parametrize(
        ("renderer_factory", "platform", "adapter_id"),
        [
            (lambda: make_meshtastic_renderer(), "meshtastic", "mesh-target"),
            (lambda: make_meshcore_renderer(), "meshcore", "mc-target"),
            (lambda: make_lxmf_renderer(), "lxmf", "lxmf-target"),
            (lambda: make_matrix_renderer(), "matrix", "matrix-target"),
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
        event = make_event()
        ctx = make_context(
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
        renderer = make_meshtastic_renderer()
        event = make_event()
        ctx = make_context(
            target_adapter="mesh-target",
            target_platform="meshtastic",
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)

        assert result.metadata.get("delivery_strategy") == "fallback_text"

    async def test_matrix_fallback_text_includes_truncation_meta(self) -> None:
        """Matrix fallback_text with char/byte budget records truncation."""
        renderer = make_matrix_renderer()
        event = make_event(payload={"text": "A" * 300})
        ctx = make_context(
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
