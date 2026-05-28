"""Immutable rendering evidence snapshot for replay-readiness.

A :class:`RenderingEvidence` captures the decision inputs and observable
outcomes of a single rendering pass without duplicating rendered payloads.
It is produced once by the pipeline after a renderer returns and attached
to the :class:`~medre.core.rendering.renderer.RenderingResult`.

**Design constraints**

* Immutable (frozen dataclass).
* JSON-safe via :meth:`to_dict` — all values are plain strs, ints, bools,
  or ``None``.
* No payload duplication — only *metrics* (character/byte counts) are
  recorded, never the rendered text itself.
* Individual renderers do not produce evidence; the pipeline builds it.

Public symbols
--------------
* :class:`RenderingEvidence` – frozen evidence snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Module-level literal type aliases defined before any class in renderer.py
# — safe to import even when renderer.py is mid-load during circular import.
from medre.core.rendering.renderer import (
    CapabilityLevel,
    DeliveryStrategyMethod,
    FallbackApplied,
)

if TYPE_CHECKING:
    from medre.core.rendering.renderer import RenderingContext, RenderingResult

#: Evidence schema version.  Bumped on backward-incompatible field changes.
_EVIDENCE_SCHEMA_VERSION: str = "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_char_byte_metrics(
    payload: dict[str, object],
    metadata: dict[str, object],
) -> tuple[int | None, int | None, int | None]:
    """Derive normalised text metrics from *payload* and *metadata*.

    Returns ``(rendered_chars, rendered_bytes, original_chars)``.
    Any unavailable metric is ``None``.
    """
    rendered_chars: int | None = None
    rendered_bytes: int | None = None

    text = payload.get("text")
    if isinstance(text, str):
        rendered_chars = len(text)
        rendered_bytes = len(text.encode("utf-8"))

    original_chars: int | None = None
    raw_orig = metadata.get("original_length")
    if isinstance(raw_orig, int):
        original_chars = raw_orig

    return rendered_chars, rendered_bytes, original_chars


# ---------------------------------------------------------------------------
# RenderingEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderingEvidence:
    """Immutable snapshot of rendering decision inputs and outcomes.

    Built by
    :meth:`RenderingPipeline.render()
    <medre.core.rendering.renderer.RenderingPipeline.render>`
    after a renderer returns and attached to the resulting
    :class:`~medre.core.rendering.renderer.RenderingResult`
    via :func:`dataclasses.replace`.

    **Fields classification**

    *Decision inputs* (from :class:`RenderingContext`):
        ``renderer``, ``delivery_strategy``, ``target_adapter``,
        ``target_platform``, ``target_channel``, ``max_text_chars``,
        ``max_text_bytes``, ``capability_level``, ``capability_policy``.

    *Outputs / diagnostics* (from :class:`RenderingResult`):
        ``fallback_applied``, ``truncated``.

    *Derived metrics*:
        ``rendered_text_chars``, ``rendered_text_bytes``,
        ``original_text_chars``.

    Attributes
    ----------
    schema_version:
        Evidence schema version string (currently ``"1"``).
    renderer:
        Name of the renderer that produced the result.
    delivery_strategy:
        The resolved delivery strategy method.
    target_adapter:
        Target adapter routing identifier.
    target_platform:
        Platform name, or ``None`` if unknown.
    target_channel:
        Target channel / conversation, or ``None``.
    max_text_chars:
        Character budget from adapter capabilities, or ``None``.
    max_text_bytes:
        UTF-8 byte budget from adapter capabilities, or ``None``.
    capability_level:
        Capability level from the rendering context.  Reflects what the
        context carried — the default pipeline does **not** populate this
        from adapter capabilities, so it is typically ``"native"``.
    capability_policy:
        Optional policy hint from the context, or ``None``.  Reserved
        for forward compatibility; the default pipeline does not set it.
    fallback_applied:
        Fallback reason tag, or ``None`` when no fallback was applied.
    truncated:
        Whether the rendered content was truncated.
    rendered_text_chars:
        Character length of the rendered text payload, or ``None`` if
        the payload does not contain a ``"text"`` field.
    rendered_text_bytes:
        UTF-8 byte length of the rendered text payload, or ``None``.
    original_text_chars:
        Character length of the original (pre-truncation) text as
        reported in result metadata, or ``None`` when unavailable.
    """

    # --- Schema ---
    schema_version: str

    # --- Decision inputs (RenderingContext) ---
    renderer: str
    delivery_strategy: DeliveryStrategyMethod
    target_adapter: str
    target_platform: str | None
    target_channel: str | None
    max_text_chars: int | None
    max_text_bytes: int | None
    capability_level: CapabilityLevel
    capability_policy: str | None

    # --- Outputs / diagnostics (RenderingResult) ---
    fallback_applied: FallbackApplied | None
    truncated: bool

    # --- Derived text metrics ---
    rendered_text_chars: int | None
    rendered_text_bytes: int | None
    original_text_chars: int | None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_context_and_result(
        cls,
        renderer_name: str,
        ctx: RenderingContext,
        result: RenderingResult,
    ) -> RenderingEvidence:
        """Build evidence from a rendering context and its result.

        Parameters
        ----------
        renderer_name:
            Name of the renderer that produced *result* (from
            ``renderer.name`` in the pipeline loop).
        ctx:
            The frozen rendering context used for this render call.
        result:
            The rendering result returned by the renderer.
        """
        rendered_chars, rendered_bytes, original_chars = (
            _text_char_byte_metrics(result.payload, result.metadata)
        )

        return cls(
            schema_version=_EVIDENCE_SCHEMA_VERSION,
            renderer=renderer_name,
            delivery_strategy=ctx.delivery_strategy,
            target_adapter=ctx.target_adapter,
            target_platform=ctx.target_platform,
            target_channel=ctx.target_channel,
            max_text_chars=ctx.max_text_chars,
            max_text_bytes=ctx.max_text_bytes,
            capability_level=ctx.capability_level,
            capability_policy=ctx.capability_policy,
            fallback_applied=result.fallback_applied,
            truncated=result.truncated,
            rendered_text_chars=rendered_chars,
            rendered_text_bytes=rendered_bytes,
            original_text_chars=original_chars,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe plain dict representation.

        ``capability_policy`` is omitted when ``None`` to reduce noise
        (the default pipeline never sets it).
        """
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "renderer": self.renderer,
            "delivery_strategy": self.delivery_strategy,
            "target_adapter": self.target_adapter,
            "target_platform": self.target_platform,
            "target_channel": self.target_channel,
            "max_text_chars": self.max_text_chars,
            "max_text_bytes": self.max_text_bytes,
            "capability_level": self.capability_level,
            "fallback_applied": self.fallback_applied,
            "truncated": self.truncated,
            "rendered_text_chars": self.rendered_text_chars,
            "rendered_text_bytes": self.rendered_text_bytes,
            "original_text_chars": self.original_text_chars,
        }
        if self.capability_policy is not None:
            d["capability_policy"] = self.capability_policy
        return d
