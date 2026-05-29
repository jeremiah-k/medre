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
* :class:`~medre.core.rendering.renderer.RenderingContext` –
  frozen context for a single render invocation.
* :class:`~medre.core.rendering.renderer.CapabilityLevel` –
  adapter capability levels (native, fallback_text, unsupported).
* :class:`~medre.core.rendering.renderer.DeliveryStrategyMethod` –
  well-known delivery strategy method values.
* :class:`~medre.core.rendering.renderer.FallbackApplied` –
  fallback reason tag on rendering results.
* :class:`~medre.core.rendering.evidence.RenderingEvidence` –
  immutable evidence snapshot attached to rendering results.
* :class:`~medre.core.rendering.text.TextRenderer` – concrete
  renderer for plain-text targets.
* :func:`~medre.core.rendering.text_helpers.extract_relation_text` –
  extract raw text with relation prefixes.
* :func:`~medre.core.rendering.text_helpers.truncate_text` –
  cap text at a character limit.
* :func:`~medre.core.rendering.text_helpers.truncate_text_bytes` –
  cap text at a UTF-8 byte limit.
"""

from medre.core.rendering.evidence import RenderingEvidence
from medre.core.rendering.renderer import (
    CapabilityLevel,
    DeliveryStrategyMethod,
    FallbackApplied,
    Renderer,
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.rendering.text import TextRenderer
from medre.core.rendering.text_helpers import (
    extract_relation_text,
    truncate_text,
    truncate_text_bytes,
)

__all__ = [
    "CapabilityLevel",
    "DeliveryStrategyMethod",
    "FallbackApplied",
    "Renderer",
    "RenderingContext",
    "RenderingEvidence",
    "RenderingPipeline",
    "RenderingResult",
    "TextRenderer",
    "extract_relation_text",
    "truncate_text",
    "truncate_text_bytes",
]
