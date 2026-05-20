"""Core rendering package for the medre.

This package separates target-specific *rendering* (converting a canonical
event into an adapter-ready payload) from both transforms and adapters.

Package-level imports
---------------------
* :class:`~medre.core.rendering.renderer.Renderer` – protocol
  every renderer must satisfy.
* :class:`~medre.core.rendering.renderer.RenderingPipeline` –
  ordered dispatcher across registered renderers.
* :class:`~medre.core.rendering.renderer.RenderingResult` –
  output of a rendering pass.
* :class:`~medre.core.rendering.text.TextRenderer` – concrete
  renderer for plain-text targets.
"""

from medre.core.rendering.renderer import (
    Renderer,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer

# Internal alias: the rendering result for text-based rendering paths.
# Importers can use either ``RenderingResult`` or ``TextRenderingResult``.
TextRenderingResult = RenderingResult

__all__ = [
    "Renderer",
    "RenderingPipeline",
    "RenderingResult",
    "TextRenderer",
    "TextRenderingResult",
]
