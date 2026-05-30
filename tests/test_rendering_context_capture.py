"""RenderingContext field capture tests for all transport renderers.

Covers:
- RenderingResult captures target_adapter from context.
- RenderingResult captures event_id from source event.
- RenderingResult captures target_channel from context.
- Result metadata includes renderer name identifier.
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
# RenderingContext field capture in metadata
# ===================================================================


class TestRenderingContextCapture:
    """Each renderer records context-derived fields in its result metadata
    or directly on the RenderingResult so that evidence is available
    without re-parsing the payload."""

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
    async def test_result_captures_target_adapter(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.target_adapter matches the context."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.target_adapter == adapter_id

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
    async def test_result_captures_event_id(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.event_id matches the source event."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event(event_id="evt-unique-42")
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert result.event_id == "evt-unique-42"

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
    async def test_result_captures_target_channel(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """RenderingResult.target_channel reflects the context."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
            target_channel="3",
        )
        result = await renderer.render(event, ctx)
        assert result.target_channel == "3"

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
    async def test_metadata_includes_renderer_name(
        self,
        renderer_factory: object,
        platform: str,
        adapter_id: str,
    ) -> None:
        """Result metadata includes the renderer's name identifier."""
        renderer = renderer_factory()  # type: ignore[operator]
        event = make_event()
        ctx = make_context(
            target_adapter=adapter_id,
            target_platform=platform,
        )
        result = await renderer.render(event, ctx)
        assert "renderer" in result.metadata
        assert isinstance(result.metadata["renderer"], str)
        assert result.metadata["renderer"] == renderer.name
