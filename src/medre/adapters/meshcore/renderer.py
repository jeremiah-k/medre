"""MeshCore renderer for target-specific event rendering.

The :class:`MeshCoreRenderer` converts canonical events into
MeshCore-ready content payloads (dicts with ``text`` and
``channel_index``).

The renderer is initialised with a **required** mapping of adapter IDs to
:class:`~medre.config.adapters.meshcore.MeshCoreConfig` instances.
At render time the config for *target_adapter* is resolved from this
mapping — there is no fallback or default.  An empty mapping raises
:class:`ValueError` at construction; an unknown *target_adapter* raises
:class:`KeyError` at render time.

This renderer is owned by the MeshCore adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshcore"``, the renderer
matches on that platform string directly.  This decouples renderer
selection from adapter naming conventions.

**Strict RenderingContext protocol**

Both ``can_render`` and ``render`` accept a frozen
:class:`~medre.core.rendering.renderer.RenderingContext` carrying all
dispatch metadata — delivery strategy, target identity, capability
constraints, and text budgets.  No legacy signature parameters.

**fallback_text strategy**

When ``delivery_strategy == "fallback_text"``, relation semantics are
degraded into inline text within the MeshCore content body while
preserving MeshCore payload ownership (``text``, ``channel_index``).
Contact/channel/destination semantics and target
addressing are retained in the native MeshCore structure.  The result
carries ``fallback_applied="strategy_fallback_text"``.

Text messages are supported.  UTF-8 byte-budget truncation is applied
after final text rendering using :attr:`MeshCoreConfig.max_text_bytes`.
Multi-byte UTF-8 codepoints are never split.  Truncation metadata and
the ``RenderingResult.truncated`` flag report whether the text was
trimmed.

**Important**: local send acceptance is not delivery confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from medre.adapters._attribution_dispatch import project_source_fields
from medre.core.events import CanonicalEvent
from medre.core.rendering.attribution import (
    build_relay_attribution,
    format_relay_prefix,
)
from medre.core.rendering.relations import degrade_relations_inline
from medre.core.rendering.renderer import RenderingContext, RenderingResult

if TYPE_CHECKING:
    from medre.config.adapters.meshcore import MeshCoreConfig


class MeshCoreRenderer:
    """Renderer for MeshCore transport targets.

    Produces content dicts with ``text`` and ``channel_index``.

    **Target-aware rendering.** The renderer is initialised with a
    mapping of adapter IDs to :class:`~medre.config.adapters.meshcore.MeshCoreConfig`
    instances.  At render time the config for *target_adapter* is resolved
    from this mapping.  This allows multi-node setups where each adapter
    has different ``max_text_bytes`` values.

    An empty *configs* mapping raises :class:`ValueError`.  An unknown
    *target_adapter* at render time raises :class:`KeyError`.

    Selection is via the pipeline's platform registry.
    """

    name: str = "meshcore"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshcore"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        *,
        configs: Mapping[str, MeshCoreConfig],
        source_attribution: dict[str, Any] | None = None,
    ) -> None:
        if not configs:
            raise ValueError(
                "MeshCoreRenderer requires at least one adapter config. "
                "Pass a non-empty configs mapping."
            )
        self._configs: dict[str, MeshCoreConfig] = dict(configs)
        self._source_attribution: dict[str, Any] = dict(source_attribution or {})

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` when the context's target platform is ``"meshcore"``
        and *target_adapter* has a registered config.

        Parameters
        ----------
        event:
            The canonical event to check.
        ctx:
            Frozen rendering context with target identity, delivery
            strategy, and capability metadata.

        Returns
        -------
        bool
            Whether this renderer handles events for the given context.
        """
        return (
            ctx.target_platform == self._PLATFORM
            and ctx.target_adapter in self._configs
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render a canonical event into a MeshCore content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload, truncated
          to the configured ``max_text_bytes`` UTF-8 byte budget.
        * ``channel_index``: parsed from ``ctx.target_channel`` or
          ``config.default_channel``.

        Under ``ctx.delivery_strategy == "fallback_text"``, relation
        semantics are degraded into inline text while the payload retains
        MeshCore-native structure (``text``, ``channel_index``).
        Contact/channel/destination semantics and
        target addressing are preserved.

        **Target-aware config resolution.** The renderer resolves the
        config for ``ctx.target_adapter`` from the ``configs`` mapping
        supplied at construction.  If ``ctx.target_adapter`` is not
        found, a :class:`KeyError` is raised — there is no fallback.

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
            The rendered MeshCore content dict wrapped in a result.
        """
        # Resolve target-adapter-specific config.
        target_adapter = ctx.target_adapter
        try:
            adapter_config = self._configs[target_adapter]
        except KeyError:
            raise KeyError(
                f"No MeshCoreConfig registered for target_adapter "
                f"{target_adapter!r}. Known adapters: "
                f"{sorted(self._configs.keys())}"
            ) from None

        # Use context budget if provided, else adapter config budget.
        # Clamp to non-negative to guard against misconfiguration.
        selected_max_text_bytes = (
            ctx.max_text_bytes
            if ctx.max_text_bytes is not None
            else adapter_config.max_text_bytes
        )
        max_text_bytes = max(0, selected_max_text_bytes)

        text = str(event.payload.get("text", event.payload.get("body", "")))

        # Parse channel index from ctx.target_channel
        try:
            channel_index = int(ctx.target_channel)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            channel_index = adapter_config.default_channel

        # Determine fallback behaviour
        is_fallback = ctx.delivery_strategy == "fallback_text"

        if is_fallback:
            text = self._degrade_relations_inline(event, text)

        # -- Optional relay prefix (prepended before truncation) ---------
        prefix_template = adapter_config.meshcore_relay_prefix
        rendered_prefix = ""
        prefix_formatting_error: str | None = None
        prefix_vars_used: tuple[str, ...] = ()
        prefix_vars_missing: tuple[str, ...] = ()
        prefix_vars_unknown: tuple[str, ...] = ()

        if prefix_template:
            # Resolve origin_label precedence:
            # ctx.source_origin_label (route/context) > adapter registry > None.
            source_origin_label: str | None = ctx.source_origin_label
            if not source_origin_label:
                src_attr_cfg = self._source_attribution.get(event.source_adapter)
                if src_attr_cfg is not None:
                    source_origin_label = getattr(src_attr_cfg, "origin_label", None)

            # Project source identity from native metadata via dispatch.
            native_data: dict[str, object] = {}
            if event.metadata and event.metadata.native:
                native_data = dict(event.metadata.native.data)

            projected = project_source_fields(
                native_data,
                source_adapter=event.source_adapter,
                source_transport_id=event.source_transport_id,
            )

            attr = build_relay_attribution(
                event,
                source_origin_label=source_origin_label,
                projected_fields=projected,
            )
            prefix_result = format_relay_prefix(prefix_template, attr)
            rendered_prefix = prefix_result.rendered_prefix
            prefix_formatting_error = prefix_result.formatting_error
            prefix_vars_used = prefix_result.variables_used
            prefix_vars_missing = prefix_result.missing_variables
            prefix_vars_unknown = prefix_result.unknown_variables
            # On formatting_exception the rendered_prefix is the raw
            # template — do NOT prepend it to user-facing text.
            if not (
                prefix_formatting_error
                and prefix_formatting_error.startswith("formatting_exception:")
            ):
                text = rendered_prefix + text

        # -- UTF-8 byte-budget truncation after final rendering ------
        truncated_text, was_truncated, original_bytes, rendered_bytes = (
            self._truncate_utf8_bytes(text, max_text_bytes)
        )

        content: dict[str, object] = {
            "text": truncated_text,
            "channel_index": channel_index,
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(text),
            "rendered_length": len(truncated_text),
            "original_text_bytes": original_bytes,
            "rendered_text_bytes": rendered_bytes,
            "max_text_bytes": max_text_bytes,
            "truncated": was_truncated,
        }

        # Prefix-specific metadata (only when prefix is configured).
        if prefix_template:
            metadata["relay_prefix_template"] = prefix_template
            metadata["relay_prefix_rendered"] = rendered_prefix
            metadata["relay_prefix_variables_used"] = prefix_vars_used
            metadata["relay_prefix_missing_variables"] = prefix_vars_missing
            metadata["relay_prefix_unknown_variables"] = prefix_vars_unknown
            metadata["relay_prefix_formatting_error"] = prefix_formatting_error

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=ctx.target_channel,
            payload=content,
            metadata=metadata,
            truncated=was_truncated,
            fallback_applied="strategy_fallback_text" if is_fallback else None,
        )

    # ------------------------------------------------------------------
    # Private helpers
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

    @staticmethod
    def _truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool, int, int]:
        """Truncate *text* to at most *max_bytes* UTF-8 bytes.

        Parameters
        ----------
        text:
            The text to potentially truncate.
        max_bytes:
            Maximum number of UTF-8 bytes allowed.  Must be >= 0.

        Returns
        -------
        tuple[str, bool, int, int]
            ``(truncated_text, was_truncated, original_byte_count,
            rendered_byte_count)``.
        """
        encoded = text.encode("utf-8")
        original_bytes = len(encoded)

        if max_bytes == 0:
            return ("", original_bytes > 0, original_bytes, 0)

        if original_bytes <= max_bytes:
            return (text, False, original_bytes, original_bytes)

        # Slice to byte budget and decode with errors="ignore" to
        # avoid splitting multi-byte UTF-8 codepoints.
        truncated_bytes = encoded[:max_bytes]
        truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
        rendered_bytes = len(truncated_text.encode("utf-8"))

        return (truncated_text, True, original_bytes, rendered_bytes)
