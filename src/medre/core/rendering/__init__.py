"""Core rendering package for the medre.

This package separates target-specific *rendering* (converting a canonical
event into an adapter-ready payload) from both transforms and adapters.

Convenience re-exports
----------------------
These names are re-exported from their canonical submodules for
ergonomic import paths (``from medre.core.rendering import Renderer``).
Importers may also use the longer submodule form directly.

Exported names
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
    CapabilityLevel,
    DeliveryStrategyMethod,
    Renderer,
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer

__all__ = [
    "CapabilityLevel",
    "DeliveryStrategyMethod",
    "Renderer",
    "RenderingContext",
    "RenderingPipeline",
    "RenderingResult",
    "TextRenderer",
]
