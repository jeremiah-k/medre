"""Core rendering package for the meshnet framework.

This package separates target-specific *rendering* (converting a canonical
event into an adapter-ready payload) from both transforms and adapters.

Public symbols
--------------
* :class:`~meshnet_framework.core.rendering.renderer.Renderer` – protocol
  every renderer must satisfy.
* :class:`~meshnet_framework.core.rendering.renderer.RenderingPipeline` –
  ordered dispatcher across registered renderers.
* :class:`~meshnet_framework.core.rendering.renderer.RenderingResult` –
  output of a rendering pass.
* :class:`~meshnet_framework.core.rendering.text.TextRenderer` – concrete
  renderer for plain-text targets.
"""

from meshnet_framework.core.rendering.renderer import (
    RenderingPipeline,
    RenderingResult,
    Renderer,
)
from meshnet_framework.core.rendering.text import TextRenderer

# Public alias: the rendering result for text-based rendering paths.
# Importers can use either ``RenderingResult`` or ``TextRenderingResult``.
TextRenderingResult = RenderingResult

__all__ = [
    "Renderer",
    "RenderingPipeline",
    "RenderingResult",
    "TextRenderer",
    "TextRenderingResult",
]
