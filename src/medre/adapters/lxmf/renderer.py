"""LXMF renderer for target-specific event rendering.

The :class:`LxmfRenderer` converts canonical events into
LXMF-ready content payloads (dicts with ``content``, ``title``,
``fields``, and ``destination_hash``).

This renderer is owned by the LXMF adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"lxmf"``, the renderer
matches on that platform string directly.

**Target-aware rendering**

The renderer is initialised with an optional mapping of adapter IDs to
:class:`~medre.config.adapters.lxmf.LxmfConfig` instances.  At render
time the config for *ctx.target_adapter* is resolved from this mapping
— the ``lxmf_relay_prefix`` from the target adapter's config is used
as the prefix template.  This allows multi-LXMF setups where each
adapter has a different relay prefix.

When no configs mapping is provided or the target adapter is not found,
the renderer falls back to the ``relay_prefix`` constructor argument
for backward compatibility with direct-constructed renderer instances
(e.g. in tests).

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

from dataclasses import replace
from typing import Any, Mapping

from medre.adapters.lxmf.fields import LxmfFieldsHelper
from medre.core.events import CanonicalEvent
from medre.core.rendering.attribution import (
    RelayAttribution,
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

    **Target-aware rendering.** The renderer is initialised with an
    optional mapping of adapter IDs to LXMF config instances.  At render
    time the config for *ctx.target_adapter* is resolved from this
    mapping.  The ``lxmf_relay_prefix`` from the resolved config is used
    as the prefix template.  This allows multi-LXMF setups where each
    adapter has a different relay prefix.

    When no *configs* mapping is provided or the target adapter is not
    found, the renderer falls back to *relay_prefix* for backward
    compatibility with direct-constructed renderer instances.

    When ``metadata_embedding`` is enabled (default), the renderer
    embeds a MEDRE metadata envelope in the ``fields`` dict containing the
    event ID, relations, and metadata keys.

    Selection is via the pipeline's platform registry.

    Parameters
    ----------
    configs:
        Optional mapping of adapter IDs to config objects.  When
        provided, the prefix template is resolved from the target
        adapter's ``lxmf_relay_prefix`` at render time.
    source_attribution:
        Optional mapping of adapter IDs to source attribution config
        objects.  Used to resolve ``origin_label`` for the source
        adapter when formatting relay prefixes.
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
    relay_prefix:
        Fallback template string for human-readable relay attribution
        prefix.  Used when *configs* is ``None`` or the target adapter
        is not found in the mapping.  Default ``""`` (no prefix).
    """

    name: str = "lxmf"
    """Platform name this renderer handles."""

    _PLATFORM: str = "lxmf"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        metadata_embedding: bool = True,
        relay_prefix: str = "",
        *,
        configs: Mapping[str, Any] | None = None,
        source_attribution: dict[str, Any] | None = None,
    ) -> None:
        self._configs: dict[str, Any] = dict(configs or {})
        self._source_attribution: dict[str, Any] = dict(source_attribution or {})
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

        # Optional human-readable relay prefix.  Resolved from target
        # adapter config (target-aware) with fallback to constructor
        # relay_prefix for backward compat.  Extracted from event
        # attribution and formatted via the shared prefix formatter.
        # The prefix is for human readability only — the MEDRE metadata
        # envelope in fields remains the authoritative provenance source.
        prefix_meta: dict[str, object] = {}
        prefix_template = self._resolve_prefix_template(ctx)
        if prefix_template:
            attr = self._extract_attribution_with_source(event)
            prefix_result = format_relay_prefix(prefix_template, attr)
            if prefix_result.rendered_prefix:
                text = prefix_result.rendered_prefix + text
            prefix_meta["relay_prefix_template"] = prefix_result.template_used
            prefix_meta["relay_prefix_rendered"] = prefix_result.rendered_prefix
            prefix_meta["relay_prefix_variables_used"] = prefix_result.variables_used
            prefix_meta["relay_prefix_missing_variables"] = (
                prefix_result.missing_variables
            )
            prefix_meta["relay_prefix_unknown_variables"] = (
                prefix_result.unknown_variables
            )
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
    # Prefix resolution helpers
    # ------------------------------------------------------------------

    def _resolve_prefix_template(self, ctx: RenderingContext) -> str:
        """Resolve the relay prefix template for the target adapter.

        Looks up the target adapter in the configs mapping and returns
        its ``lxmf_relay_prefix``.  Falls back to ``self._relay_prefix``
        when the configs mapping is empty or the target adapter is not
        found (backward compat for direct-constructed renderers).

        Parameters
        ----------
        ctx:
            Frozen rendering context with target adapter identity.

        Returns
        -------
        str
            The prefix template string (may be empty).
        """
        if self._configs:
            adapter_config = self._configs.get(ctx.target_adapter)
            if adapter_config is not None:
                return getattr(adapter_config, "lxmf_relay_prefix", "")
        return self._relay_prefix

    def _extract_attribution_with_source(
        self, event: CanonicalEvent
    ) -> RelayAttribution:
        """Extract relay attribution, enriching with source origin_label.

        Extracts standard relay attribution from the event, then looks
        up the source adapter in the source_attribution registry to
        populate ``source_origin_label``.

        Parameters
        ----------
        event:
            The canonical event to extract attribution from.

        Returns
        -------
        RelayAttribution
            Attribution snapshot with origin_label populated from the
            source_attribution registry when available.
        """
        attr = extract_relay_attribution(event)

        # Enrich with origin_label from source_attribution registry.
        source_info = self._source_attribution.get(event.source_adapter)
        if source_info is not None:
            origin_label = getattr(source_info, "origin_label", "") or ""
            meshnet_name = getattr(source_info, "meshnet_name", "") or ""
            if origin_label:
                attr = replace(attr, source_origin_label=origin_label)
            if meshnet_name:
                attr = replace(attr, source_meshnet_name=meshnet_name)

        return attr

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
