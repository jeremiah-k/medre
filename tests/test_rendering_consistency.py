"""Cross-renderer consistency tests for all four transport renderers.

Covers:
- All renderers return a RenderingResult instance.
- All results preserve source event_id.
- All results have non-empty metadata dict.
- All results have non-empty payload dict.
- All fallback_text results include fallback_applied evidence.
"""

from __future__ import annotations

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
# Renderer consistency across all four transports
# ===================================================================


class TestRendererConsistency:
    """All four transport renderers include evidence consistently
    in their RenderingResult."""

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
    async def test_all_renderers_return_rendering_result(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every renderer returns a RenderingResult."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert isinstance(result, RenderingResult)

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
    async def test_all_results_have_event_id(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult preserves the source event_id."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event(event_id="evt-consistency-99")
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.event_id == "evt-consistency-99"

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
    async def test_all_results_have_non_empty_metadata(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult has non-empty metadata."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.metadata
        assert isinstance(result.metadata, dict)

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
    async def test_all_results_have_payload(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Every RenderingResult has a non-empty payload dict."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.payload
        assert isinstance(result.payload, dict)

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
    async def test_all_fallback_results_include_evidence(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """All renderers under fallback_text record fallback_applied."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            delivery_strategy="fallback_text",
        )
        result = await renderer.render(event, ctx)
        # Core evidence: fallback_applied marker + renderer identity
        assert result.fallback_applied == "strategy_fallback_text"
        assert result.metadata.get("renderer") is not None
