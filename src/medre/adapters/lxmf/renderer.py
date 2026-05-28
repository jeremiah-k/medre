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
from medre.core.rendering.renderer import RenderingContext, RenderingResult


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
    """

    name: str = "lxmf"
    """Platform name this renderer handles."""

    _PLATFORM: str = "lxmf"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        metadata_embedding: bool = True,
    ) -> None:
        self._metadata_embedding = metadata_embedding

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
        text = str(event.payload.get("body", event.payload.get("text", "")))
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

        content: dict[str, object] = {
            "content": text,
            "title": title,
            "fields": fields,
            "destination_hash": "",
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
        }

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload=content,
            metadata=metadata,
            truncated=False,
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

        Appends human-readable relation descriptions to *text* so that
        relation information is preserved in the content body when
        native relation handling is unavailable.

        Ensures the result is non-empty when relation data exists,
        preventing false sent-receipt appearance from empty content.

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
        if not event.relations:
            return text

        parts: list[str] = []
        for rel in event.relations:
            target = (
                rel.fallback_text
                or rel.target_event_id
                or (
                    rel.target_native_ref.native_message_id
                    if rel.target_native_ref is not None
                    else None
                )
                or "?"
            )
            if rel.relation_type == "reply":
                parts.append(f"[reply to: {target}]")
            elif rel.relation_type == "reaction":
                emoji = rel.key or "∟"
                parts.append(f"[reaction {emoji} to: {target}]")
            elif rel.relation_type == "edit":
                parts.append(f"[edit of: {target}]")
            elif rel.relation_type == "delete":
                parts.append(f"[delete of: {target}]")
            elif rel.relation_type == "thread":
                parts.append(f"[thread on: {target}]")
            else:
                parts.append(f"[{rel.relation_type}: {target}]")

        inline = " ".join(parts)
        if text:
            return f"{text} {inline}"
        return inline
