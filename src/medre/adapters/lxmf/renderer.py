"""LXMF renderer for target-specific event rendering.

The :class:`LxmfRenderer` converts canonical events into
LXMF-ready content payloads (dicts with ``content``, ``title``,
``fields``, and ``destination_hash``).

This renderer is owned by the LXMF adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"lxmf"``, the renderer
matches on that platform string directly.

**Strict RenderingContext protocol**

Both ``can_render`` and ``render`` accept a frozen
:class:`~medre.core.rendering.renderer.RenderingContext` carrying all
dispatch metadata — delivery strategy, target identity, capability
constraints, and text budgets.  No legacy signature parameters.

**fallback_text strategy**

When ``delivery_strategy == "fallback_text"``, relation semantics are
degraded into inline text within the LXMF content field while
preserving LXMF payload ownership (content, title, fields,
destination_hash).  The MEDRE fields envelope omits structured
relations (``relations=()``) — the only relation representation is
the inline text in the content field.  The result carries
``fallback_applied="strategy_fallback_text"``.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.core.events import CanonicalEvent
from medre.core.rendering.attribution import (
    extract_relay_attribution,
    format_relay_prefix,
)
from medre.core.rendering.relations import degrade_relations_inline
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from medre.core.rendering.text_helpers import truncate_text as _truncate_text


class LxmfRenderer:
    """Renderer for LXMF transport targets.

    Produces content dicts with ``content``, ``title``, ``fields``, and
    ``destination_hash``.

    When ``metadata_embedding`` is enabled (default), the renderer
    embeds a MEDRE envelope in the ``fields`` dict containing the
    event ID, relations, and metadata keys.

    Selection is via the pipeline's platform registry.

    Parameters
    ----------
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
    relay_prefix:
        Optional template string for human-readable relay attribution
        prefix.  When non-empty, the shared prefix formatter resolves
        ``{placeholder}`` syntax against relay attribution extracted
        from the event.  Default ``""`` (no prefix).
    """

    name: str = "lxmf"
    """Platform name this renderer handles."""

    _PLATFORM: str = "lxmf"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        metadata_embedding: bool = True,
        relay_prefix: str = "",
    ) -> None:
        self._metadata_embedding = metadata_embedding
        self._relay_prefix = relay_prefix

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` when the context's target platform is ``"lxmf"``.

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        ctx:
            Frozen rendering context with target identity, delivery
            strategy, and capability metadata.

        Returns
        -------
        bool
            Whether this renderer handles events for the given context.
        """
        return ctx.target_platform == self._PLATFORM

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render a canonical event into an LXMF content payload.

        Under ``ctx.delivery_strategy == "fallback_text"``, relation
        semantics are degraded into inline text while the payload retains
        LXMF-native structure (content, title, fields, destination_hash).

        Parameters
        ----------
        event:
            The canonical event to render.
        ctx:
            Frozen rendering context with target identity, delivery
            strategy, capability metadata, and text budgets.

        Returns
        -------
        RenderingResult
            The rendered LXMF content dict wrapped in a result.
        """
        text = str(event.payload.get("text", event.payload.get("body", "")))
        title = str(event.payload.get("title", ""))

        # Determine fallback behaviour early — controls envelope relations
        is_fallback = ctx.delivery_strategy == "fallback_text"

        # Build fields dict with optional MEDRE envelope.
        # Under fallback_text, relations are degraded to inline text only;
        # the envelope carries an empty relations list to avoid duplicating
        # relation data as both structured fields and inline text.
        fields: dict[int, Any] = {}

        if self._metadata_embedding:
            meta_keys: dict[str, Any] = {}
            if event.metadata.native is not None:
                meta_keys = dict(event.metadata.native.data)
            fields = LxmfFieldsHelper.embed_envelope(
                fields=fields,
                event_id=event.event_id,
                relations=() if is_fallback else event.relations,
                metadata=meta_keys,
                source_adapter=event.source_adapter,
                source_transport_id=event.source_transport_id,
                source_channel_id=event.source_channel_id,
                lineage=event.lineage,
            )

        if is_fallback:
            text = self._degrade_relations_inline(event, text)

        # Optional human-readable relay prefix.  Extracted from event
        # attribution and formatted via the shared prefix formatter.
        # The prefix is for human readability only — the MEDRE metadata
        # envelope in fields remains the authoritative provenance source.
        prefix_meta: dict[str, object] = {}
        if self._relay_prefix:
            attr = extract_relay_attribution(event)
            prefix_result = format_relay_prefix(self._relay_prefix, attr)
            if prefix_result.rendered_prefix:
                text = prefix_result.rendered_prefix + text
            prefix_meta["relay_prefix_template"] = prefix_result.template_used
            prefix_meta["relay_prefix_rendered"] = prefix_result.rendered_prefix
            if prefix_result.formatting_error:
                prefix_meta["relay_prefix_formatting_error"] = (
                    prefix_result.formatting_error
                )

        # Enforce character budget declared in adapter capabilities
        # (max_text_chars=16384 by default).  Record original length
        # in metadata for evidence without duplicating the payload.
        original_length = len(text)
        truncated = False
        if ctx.max_text_chars is not None:
            text, truncated = _truncate_text(text, max_text_chars=ctx.max_text_chars)

        content: dict[str, object] = {
            "content": text,
            "title": title,
            "fields": fields,
            "destination_hash": "",
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": original_length,
        }
        metadata.update(prefix_meta)

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload=content,
            metadata=metadata,
            truncated=truncated,
            fallback_applied="strategy_fallback_text" if is_fallback else None,
        )

    # ------------------------------------------------------------------
    # Fallback helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _degrade_relations_inline(
        event: CanonicalEvent,
        text: str,
    ) -> str:
        """Degrade relation semantics into inline text.

        Delegates to the shared
        :func:`~medre.core.rendering.relations.degrade_relations_inline`
        helper.

        Parameters
        ----------
        event:
            The canonical event whose relations to degrade.
        text:
            The existing content text.

        Returns
        -------
        str
            Content text with inline relation descriptions appended.
        """
        return degrade_relations_inline(event, text)
