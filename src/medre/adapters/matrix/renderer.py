"""Matrix renderer for target-specific event rendering.

The :class:`MatrixRenderer` converts canonical events into Matrix-ready
content payloads (``m.room.message`` dicts with ``msgtype``, ``body``,
optional ``m.relates_to``, and a MEDRE metadata envelope).

This renderer is owned by the Matrix adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"matrix"``, the renderer
matches on that platform string directly.

**Supported relation types**: text messages, native replies, and
reactions (true ``m.reaction`` or MMRelay emote fallback).
"""

from __future__ import annotations

from typing import Any, Mapping

from medre.adapters._attribution_dispatch import project_source_fields
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.core.events import CanonicalEvent, EventRelation
from medre.core.rendering.attribution import (
    RelayAttribution,
    build_relay_attribution,
    format_relay_prefix,
)
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingResult,
)
from medre.core.rendering.text_helpers import (
    extract_relation_text,
    truncate_text,
    truncate_text_bytes,
)
from medre.interop.mmrelay import (
    EMOJI_FLAG_VALUE,
    KEY_EMOJI,
    KEY_ID,
    KEY_LONGNAME,
    KEY_MESHNET,
    KEY_PORTNUM,
    KEY_REACTION_KEY,
    KEY_REPLY_ID,
    KEY_SHORTNAME,
    KEY_TEXT,
    PORTNUM_TEXT,
    derive_meshnet_value,
)


class MatrixRenderer:
    """Renderer for Matrix presentation targets.

    Produces ``m.room.message`` content dicts with ``m.text`` msgtype,
    a body string, optional relation metadata (replies and reactions),
    and a MEDRE provenance envelope.

    Selection is via the pipeline's platform registry.
    """

    name: str = "matrix"

    _PLATFORM: str = "matrix"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        *,
        source_configs: Mapping[str, Any] | None = None,
        source_attribution: dict[str, Any] | None = None,
        configs: Mapping[str, Any] | None = None,
    ) -> None:
        self._source_configs: dict[str, Any] = dict(source_configs or {})
        self._source_attribution: dict[str, Any] = dict(source_attribution or {})
        self._configs: dict[str, Any] = dict(configs or {})

    # ------------------------------------------------------------------
    # Source-adapter config resolution
    # ------------------------------------------------------------------

    def _resolve_source_config(self, event: CanonicalEvent) -> Any | None:
        """Return the source adapter config for *event*, or ``None``.

        Looks up ``event.source_adapter`` in the ``source_configs`` mapping
        supplied at construction.  Returns ``None`` when no mapping is
        configured or the source adapter is not found — callers use
        empty/neutral defaults (no Meshtastic prefix or metadata).
        """
        if not self._source_configs:
            return None
        return self._source_configs.get(event.source_adapter)

    def _resolve_mmrelay_meshnet(
        self,
        event: CanonicalEvent,
        ctx_source_origin_label: str | None = None,
    ) -> str:
        """Resolve the meshnet label for mmrelay ``KEY_MESHNET``.

        Uses :func:`~medre.interop.mmrelay.derive_meshnet_value` with
        precedence: *ctx_source_origin_label* (route/context) > adapter
        ``origin_label`` from source_attribution registry > empty string.

        Parameters
        ----------
        event:
            The canonical event (used to look up adapter origin_label).
        ctx_source_origin_label:
            Route/context origin label from ``RenderingContext``.
        """
        adapter_label = self._resolve_source_origin_label(event)
        return derive_meshnet_value(ctx_source_origin_label, adapter_label)

    def _get_matrix_relay_prefix(
        self, event: CanonicalEvent, target_adapter: str = ""
    ) -> str:
        """Resolve matrix relay prefix for rendering.

        Resolution order:
        1. Target adapter config (``configs``) ``relay_prefix`` — target-local.
        2. Empty string (neutral default).
        """
        # Target-local: look up target adapter in Matrix configs
        if target_adapter and self._configs:
            target_cfg = self._configs.get(target_adapter)
            if target_cfg is not None:
                rp = getattr(target_cfg, "relay_prefix", "")
                if rp:
                    return rp
        return ""

    def _get_mmrelay_compat(self, event: CanonicalEvent) -> bool:
        """Resolve mmrelay_compatibility for *event*'s source adapter.

        Returns the config's ``mmrelay_compatibility`` when a source config
        is matched; otherwise returns ``False`` (neutral default).
        """
        cfg = self._resolve_source_config(event)
        if cfg is not None:
            return getattr(cfg, "mmrelay_compatibility", False)
        return False

    def _resolve_source_origin_label(self, event: CanonicalEvent) -> str | None:
        """Look up source origin_label from the source_attribution registry.

        Returns the ``origin_label`` for ``event.source_adapter`` when
        found in the registry; otherwise ``None``.
        """
        sa = self._source_attribution.get(event.source_adapter)
        if sa is not None:
            return getattr(sa, "origin_label", None)
        return None

    @staticmethod
    def _resolve_mmrelay_sender_names(
        native_data: dict[str, object],
    ) -> tuple[str, str]:
        """Resolve mmrelay KEY_LONGNAME / KEY_SHORTNAME from native data.

        Resolution order per field:

        1. Bare Meshtastic-native key (``longname`` / ``shortname``) —
           present for Meshtastic-origin events.
        2. Existing mmrelay wire key (``meshtastic_longname`` /
           ``meshtastic_shortname``) — preserved from external mmrelay
           Matrix event content captured by the codec.
        3. Empty string.

        Matrix ``displayname`` is intentionally **not** used — Matrix
        display names project into generic ``{sender}`` via Matrix
        attribution, not into Meshtastic-shaped mmrelay wire fields.
        """
        longname = native_data.get("longname") or native_data.get(KEY_LONGNAME) or ""
        shortname = native_data.get("shortname") or native_data.get(KEY_SHORTNAME) or ""
        return str(longname), str(shortname)

    def _build_source_attribution(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext | None = None,
    ) -> RelayAttribution:
        """Build a ``RelayAttribution`` from the source_attribution registry
        and native metadata.

        Shared helper used by both :meth:`_apply_matrix_relay_prefix` and
        :meth:`_format_reaction_prefix` to avoid duplicating the
        origin_label / platform_hint / projection logic.

        Origin_label precedence: ``ctx.source_origin_label`` (when not
        ``None``, including explicit ``""``) > adapter registry > ``None``.
        """

        source_info = self._source_attribution.get(event.source_adapter)
        source_origin_label = (
            getattr(source_info, "origin_label", None) if source_info else None
        )
        if ctx is not None and ctx.source_origin_label is not None:
            source_origin_label = ctx.source_origin_label

        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        platform_hint = getattr(source_info, "platform", None) if source_info else None
        projected = project_source_fields(
            native_data,
            source_adapter=event.source_adapter,
            source_transport_id=event.source_transport_id,
            platform_hint=platform_hint,
        )

        return build_relay_attribution(
            event,
            source_origin_label=source_origin_label,
            projected_fields=projected,
        )

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` when *ctx.target_platform* is ``"matrix"``.

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
            Whether this renderer handles events for the given adapter.
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
        """Render a canonical event into a Matrix content payload.

        The rendered payload includes:

        * ``msgtype``: ``"m.text"`` (or ``"m.emote"`` for reaction fallback)
        * ``body``: extracted text from the event payload
        * ``medre.envelope``: provenance metadata
        * ``m.relates_to``: added for replies and reactions (native mode only)

        **Strategy fallback** — when ``ctx.delivery_strategy`` is
        ``"fallback_text"``, relation semantics are degraded into plain
        text within the Matrix payload body.  Native ``m.relates_to`` and
        ``_matrix_event_type`` fields are **not** emitted.  The body is
        produced using the same deterministic wording as
        :class:`~medre.core.rendering.text.TextRenderer` so that relation
        information is preserved as readable text.  The result carries
        ``fallback_applied="strategy_fallback_text"``.

        **Native / direct mode** — replies preserve ``m.in_reply_to``
        and inject ``KEY_REPLY_ID`` from native/relation metadata when
        available.  Reactions render as true ``m.reaction`` (with
        internal ``_matrix_event_type='m.reaction'``) when a target
        event/native Matrix id is available and mmrelay_compat is false.
        When mmrelay_compat is true or the target is missing, an
        ``m.emote`` fallback is rendered with MMRelay keys.

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
            The rendered Matrix content dict wrapped in a result.
        """
        target_adapter = ctx.target_adapter
        target_channel = ctx.target_channel
        delivery_strategy = ctx.delivery_strategy
        is_fallback = delivery_strategy == "fallback_text"

        # ------------------------------------------------------------------
        # Fallback-text path: degrade relations into plain text body
        # ------------------------------------------------------------------
        if is_fallback:
            return self._render_fallback_text(event, ctx)

        # ------------------------------------------------------------------
        # Native / direct path
        # ------------------------------------------------------------------
        body = str(event.payload.get("text", event.payload.get("body", "")))

        # Determine if a reaction relation is present before applying the
        # body-level prefix — reactions manage their own prefix metadata.
        _is_reaction = (
            event.relations and event.relations[0].relation_type == "reaction"
        )

        # Apply relay prefix for mesh→Matrix direction (skip for reactions;
        # reactions produce their own prefix in the emote fallback body or
        # discard it entirely for true m.reaction annotations).
        reaction_prefix_meta: dict[str, object] = {}
        if _is_reaction:
            prefix_meta: dict[str, object] = {}
        else:
            body, prefix_meta = self._apply_matrix_relay_prefix(
                event, body, target_adapter, ctx
            )

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(body),
        }

        # Handle relations — reply and reaction
        if event.relations:
            rel = event.relations[0]

            if rel.relation_type == "reply":
                mx_event_id = self._matrix_target_event_id(rel, target_adapter)
                native_data: dict[str, object] = {}
                if event.metadata and event.metadata.native:
                    native_data = dict(event.metadata.native.data)
                # Extract MMRelay meshtastic_replyId from relation metadata
                rel_meta = getattr(rel, "metadata", {}) or {}
                mmrelay_id = rel_meta.get("meshtastic_reply_id")
                if mmrelay_id in (None, ""):
                    mmrelay_id = native_data.get(KEY_REPLY_ID)
                if mx_event_id:
                    # Matrix-native reply — render m.in_reply_to with Matrix event ID.
                    # No manual fallback quoting: Matrix clients handle display
                    # via m.relates_to.m.in_reply_to natively.
                    content["body"] = body
                    content["m.relates_to"] = {
                        "m.in_reply_to": {
                            "event_id": mx_event_id,
                        }
                    }
                # Always inject KEY_REPLY_ID when a Matrix-native target or MMRelay metadata is present
                # (used by MMRelay-compatible Matrix consumers)
                mx_reply_id = (
                    mmrelay_id if mmrelay_id not in (None, "") else mx_event_id
                )
                if mx_reply_id not in (None, ""):
                    content[KEY_REPLY_ID] = str(mx_reply_id)

            elif rel.relation_type == "reaction":
                reaction_prefix_meta = self._render_reaction(
                    rel,
                    content,
                    target_adapter,
                    event,
                    ctx,
                )

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        # Inject mmrelay-compatible metadata when enabled (skip for
        # reactions — _render_reaction already handles all MMRelay keys).
        if self._get_mmrelay_compat(event) and not _is_reaction:
            self._inject_mmrelay_metadata(event, content, ctx.source_origin_label)

        metadata: dict[str, object] = {
            "renderer": self.name,
        }
        metadata.update(prefix_meta)
        metadata.update(reaction_prefix_meta)

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            fallback_applied=None,
        )

    # ------------------------------------------------------------------
    # Fallback-text rendering
    # ------------------------------------------------------------------

    def _render_fallback_text(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render event with degraded relation text for fallback_text strategy.

        Produces a valid Matrix content payload (``msgtype``/``body``/MEDRE
        envelope) without native ``m.relates_to`` or reaction-specific
        ``_matrix_event_type`` fields.  Relation semantics are expressed as
        deterministic plain text in the ``body`` using the same wording as
        :class:`~medre.core.rendering.text.TextRenderer`.

        Sets ``fallback_applied="strategy_fallback_text"`` on the result.
        """
        # Reuse deterministic wording for degraded relations
        degraded_text = extract_relation_text(event)

        # Apply relay prefix for mesh→Matrix direction BEFORE truncation
        # so that the final body (prefix + text) respects the text budget.
        body, prefix_meta = self._apply_matrix_relay_prefix(
            event, degraded_text, ctx.target_adapter, ctx
        )

        # Truncate the final body (including relay prefix) when the
        # context imposes a text budget.
        truncated = False
        original_length = len(body)
        original_text_bytes = len(body.encode("utf-8"))
        if ctx.max_text_chars is not None:
            body, truncated = truncate_text(
                body,
                max_text_chars=ctx.max_text_chars,
            )
        # Byte-safe truncation when a byte budget is configured.
        if ctx.max_text_bytes is not None:
            body, byte_truncated, _orig_bytes, _rendered_bytes = truncate_text_bytes(
                body,
                max_text_bytes=ctx.max_text_bytes,
            )
            if byte_truncated:
                truncated = True
        rendered_text_bytes = len(body.encode("utf-8"))

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(body),
        }

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        # Inject mmrelay-compatible metadata when enabled — relation
        # rendering is degraded but transport metadata is still valid.
        if self._get_mmrelay_compat(event):
            self._inject_mmrelay_metadata(event, content, ctx.source_origin_label)

        result_metadata: dict[str, object] = {
            "renderer": self.name,
        }
        result_metadata.update(prefix_meta)
        if truncated:
            result_metadata["original_length"] = original_length
            result_metadata["original_text_bytes"] = original_text_bytes
            result_metadata["rendered_text_bytes"] = rendered_text_bytes
            if ctx.max_text_bytes is not None:
                result_metadata["max_text_bytes"] = ctx.max_text_bytes

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload=content,
            metadata=result_metadata,
            truncated=truncated,
            fallback_applied="strategy_fallback_text",
        )

    # ------------------------------------------------------------------
    # Target ID ownership
    # ------------------------------------------------------------------

    @staticmethod
    def _matrix_target_event_id(rel: Any, target_adapter: str) -> str | None:
        """Return a Matrix-native target event ID from a relation, or ``None``.

        A Matrix-native target ID is valid only when the relation's
        ``target_native_ref`` belongs to *target_adapter* and has a
        non-empty ``native_message_id``.

        The canonical ``rel.target_event_id`` is **never** used as a Matrix
        event ID — it is an internal MEDRE canonical ID, not a Matrix
        event ID.
        """
        ref = getattr(rel, "target_native_ref", None)
        if ref is None:
            return None
        adapter = getattr(ref, "adapter", None)
        if adapter != target_adapter:
            return None
        mid = getattr(ref, "native_message_id", None)
        return str(mid) if mid else None

    # ------------------------------------------------------------------
    # Reaction helpers
    # ------------------------------------------------------------------

    _REACTION_SYMBOL_FALLBACK = "\u26a0\ufe0f"  # ⚠️

    @staticmethod
    def _extract_reaction_symbol(rel: EventRelation, event: CanonicalEvent) -> str:
        """Return the reaction emoji/symbol with a fallback chain.

        Preference order: ``rel.key``, ``event.payload['key']``,
        ``event.payload['emoji']``, ``event.payload['body']``.
        Leading/trailing whitespace is stripped.  Falls back to ⚠️
        when all sources are blank.
        """
        for source in (
            rel.key,
            event.payload.get("key"),
            event.payload.get("emoji"),
            event.payload.get("body"),
        ):
            if source is not None:
                stripped = str(source).strip()
                if stripped:
                    return stripped
        return MatrixRenderer._REACTION_SYMBOL_FALLBACK

    @staticmethod
    def _extract_original_text(rel: EventRelation, event: CanonicalEvent) -> str:
        """Return the original message text preview for a reaction.

        Preference order:

        1. ``rel.metadata['meshtastic_text']`` or ``rel.metadata['text']``
        2. ``rel.fallback_text``
        3. Event native metadata ``meshtastic_text`` or ``text``
        4. Empty string
        """
        # 1. Relation metadata (set by pipeline enrichment / codec)
        rel_meta = getattr(rel, "metadata", {}) or {}
        text = rel_meta.get("meshtastic_text") or rel_meta.get("text")
        if text:
            return str(text)

        # 2. Fallback text on the relation
        if rel.fallback_text:
            return str(rel.fallback_text)

        # 3. Event native metadata fields
        if event.metadata and event.metadata.native:
            native_data = event.metadata.native.data
            text = native_data.get("meshtastic_text") or native_data.get("text")
            if text:
                return str(text)

        # 4. Empty string
        return ""

    @staticmethod
    def _abbreviate_text(text: str, max_len: int = 40) -> str:
        """Normalise newlines to spaces and truncate with ``...`` when long."""
        normalized = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        # Collapse consecutive spaces from mixed line-ending replacement
        while "  " in normalized:
            normalized = normalized.replace("  ", " ")
        if len(normalized) > max_len:
            return normalized[:max_len] + "..."
        return normalized

    def _format_reaction_prefix(
        self,
        event: CanonicalEvent,
        target_adapter: str = "",
        ctx: RenderingContext | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Format the configured relay prefix for a reaction emote body.

        Uses the shared generic attribution builder and safe prefix formatter.

        Returns a ``(prefix_str, formatter_meta)`` tuple.
        ``prefix_str`` may be empty when no prefix template is configured.
        ``formatter_meta`` contains diagnostic keys when a prefix was
        formatted, empty dict otherwise.
        """
        template = self._get_matrix_relay_prefix(event, target_adapter)
        if not template:
            return "", {}

        attr = self._build_source_attribution(event, ctx)
        fmt_result = format_relay_prefix(template, attr)

        # On internal exception, return empty prefix (preserve safety).
        if fmt_result.formatting_error and fmt_result.formatting_error.startswith(
            "formatting_exception:"
        ):
            return "", {}

        formatter_meta: dict[str, object] = {
            "relay_prefix_template": fmt_result.template_used,
            "relay_prefix_rendered": fmt_result.rendered_prefix,
            "relay_prefix_variables_used": fmt_result.variables_used,
            "relay_prefix_missing_variables": fmt_result.missing_variables,
            "relay_prefix_unknown_variables": fmt_result.unknown_variables,
            "relay_prefix_formatting_error": fmt_result.formatting_error,
        }
        return fmt_result.rendered_prefix, formatter_meta

    # ------------------------------------------------------------------
    # Reaction rendering
    # ------------------------------------------------------------------

    def _render_reaction(
        self,
        rel: EventRelation,
        content: dict[str, object],
        target_adapter: str,
        event: CanonicalEvent,
        ctx: RenderingContext | None = None,
    ) -> dict[str, object]:
        """Render a reaction relation into the Matrix content dict.

        When a Matrix-native target ID (owned by *target_adapter*) is
        available and mmrelay_compat is false, produces a true
        ``m.reaction`` via an internal ``_matrix_event_type`` key
        (consumed by the adapter).

        When mmrelay_compat is true or no Matrix-native target exists,
        falls back to an ``m.emote`` with MMRelay-compatible body and
        full mesh metadata.

        The canonical ``rel.target_event_id`` is **never** used as a
        Matrix event ID — it is an internal MEDRE canonical ID.

        Returns a dict of reaction-specific prefix metadata to be merged
        into the rendering result metadata.  Empty dict when no prefix
        metadata applies (e.g. true ``m.reaction`` annotations).
        """
        mx_event_id = self._matrix_target_event_id(rel, target_adapter)

        # Extract MMRelay reply ID from relation metadata for fallback
        rel_meta = getattr(rel, "metadata", {}) or {}

        if mx_event_id is not None and not self._get_mmrelay_compat(event):
            # True Matrix reaction — adapter will use _matrix_event_type
            # Remove default msgtype/body/format/formatted_body set at top of render()
            content.pop("msgtype", None)
            content.pop("body", None)
            content.pop("format", None)
            content.pop("formatted_body", None)
            symbol = self._extract_reaction_symbol(rel, event)
            content["m.relates_to"] = {
                "rel_type": "m.annotation",
                "event_id": mx_event_id,
                "key": symbol,
            }
            # Internal key consumed by adapter; never leaks to homeserver
            content["_matrix_event_type"] = "m.reaction"
            # True m.reaction carries no prefix metadata — body is removed.
            return {}
        else:
            # mmrelay_compat or missing Matrix-native target → m.emote fallback
            symbol = self._extract_reaction_symbol(rel, event)
            original_text = self._abbreviate_text(
                self._extract_original_text(rel, event)
            )
            prefix, _reaction_prefix_meta = self._format_reaction_prefix(
                event, target_adapter, ctx
            )

            # Store prefix metadata to return to caller for result metadata.

            if not prefix or not prefix.strip():
                emote_body = f'\n reacted {symbol} to "{original_text}"'
            elif prefix[-1].isspace():
                emote_body = f'\n {prefix}reacted {symbol} to "{original_text}"'
            else:
                emote_body = f'\n {prefix} reacted {symbol} to "{original_text}"'

            content["msgtype"] = "m.emote"
            content["body"] = emote_body
            content["formatted_body"] = self._text_to_html(emote_body)
            content[KEY_EMOJI] = EMOJI_FLAG_VALUE
            content[KEY_REACTION_KEY] = symbol

            # KEY_TEXT: original text preview (not reaction emoji)
            content[KEY_TEXT] = original_text

            # KEY_REPLY_ID: prefer meshtastic_reply_id from metadata,
            # fall back to the Matrix-native target event ID when available.
            mmrelay_reply_id = rel_meta.get("meshtastic_reply_id")
            if mmrelay_reply_id not in (None, ""):
                content[KEY_REPLY_ID] = str(mmrelay_reply_id)
            elif mx_event_id not in (None, ""):
                content[KEY_REPLY_ID] = str(mx_event_id)

            # Mesh provenance metadata
            native_data: dict[str, object] = {}
            if event.metadata and event.metadata.native:
                native_data = dict(event.metadata.native.data)

            content[KEY_ID] = str(native_data.get("packet_id", ""))
            _longname, _shortname = self._resolve_mmrelay_sender_names(native_data)
            content[KEY_LONGNAME] = _longname
            content[KEY_SHORTNAME] = _shortname
            content[KEY_MESHNET] = self._resolve_mmrelay_meshnet(
                event,
                ctx.source_origin_label if ctx is not None else None,
            )
            content[KEY_PORTNUM] = PORTNUM_TEXT

            return _reaction_prefix_meta

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text_to_html(text: str) -> str:
        """Convert plain text to a safe HTML formatted body.

        Applies HTML escaping, converts line breaks to ``<br/>``, and
        wraps the result in ``<p>`` tags.  This provides a safe baseline
        formatted body for Matrix clients that prefer HTML.
        """
        import html as _html

        # Normalize line endings before escaping so that Windows-style
        # \r\n and legacy Mac \r are both converted to \n first.
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        escaped = _html.escape(normalized, quote=False)
        # Convert line breaks to <br/>
        br = escaped.replace("\n", "<br/>")
        # Wrap in <p> tags
        return f"<p>{br}</p>"

    # ------------------------------------------------------------------
    # Relay prefix
    # ------------------------------------------------------------------

    def _apply_matrix_relay_prefix(
        self,
        event: CanonicalEvent,
        body: str,
        target_adapter: str = "",
        ctx: RenderingContext | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Prepend the configured relay prefix template to *body*.

        Uses the shared generic attribution builder and safe prefix formatter
        from :mod:`medre.core.rendering.attribution`.  Source identity is
        projected via the adapter attribution dispatch, keeping core free
        of native transport key knowledge.

        Variables available in templates include all canonical ``source_*``
        fields plus preferred aliases (``{sender}``, ``{sender_short}``,
        ``{sender_id}``, ``{origin_label}``, ``{platform}``, ``{channel}``,
        ``{route_id}``).

        Returns a ``(prefixed_body, formatter_meta)`` tuple.
        ``formatter_meta`` is empty when no prefix is configured or a
        formatting exception occurred; otherwise it contains diagnostic
        keys from :class:`PrefixFormatterResult`.
        """
        template = self._get_matrix_relay_prefix(event, target_adapter)
        if not template:
            return body, {}

        attr = self._build_source_attribution(event, ctx)
        fmt_result = format_relay_prefix(template, attr)

        # On internal exception, return body unchanged (preserve safety).
        if fmt_result.formatting_error and fmt_result.formatting_error.startswith(
            "formatting_exception:"
        ):
            return body, {}

        formatter_meta: dict[str, object] = {
            "relay_prefix_template": fmt_result.template_used,
            "relay_prefix_rendered": fmt_result.rendered_prefix,
            "relay_prefix_variables_used": fmt_result.variables_used,
            "relay_prefix_missing_variables": fmt_result.missing_variables,
            "relay_prefix_unknown_variables": fmt_result.unknown_variables,
            "relay_prefix_formatting_error": fmt_result.formatting_error,
        }
        return f"{fmt_result.rendered_prefix}{body}", formatter_meta

    # ------------------------------------------------------------------
    # mmrelay compatibility
    # ------------------------------------------------------------------

    def _inject_mmrelay_metadata(
        self,
        event: CanonicalEvent,
        content: dict[str, object],
        ctx_source_origin_label: str | None = None,
    ) -> None:
        """Embed mmrelay-compatible mesh metadata into *content*.

        When mmrelay compatibility is enabled, the Matrix content payload
        is augmented with wire-format keys that mirror the fields mmrelay
        consumers expect.  The key names come from
        :mod:`medre.interop.mmrelay` so that the wire contract lives
        outside any single adapter.

        Injected keys (see :mod:`medre.interop.mmrelay` for names):

        * packet ID from native metadata.
        * sender long name from native metadata.
        * sender short name from native metadata.
        * mesh network name from config.
        * hardcoded ``"TEXT_MESSAGE_APP"`` port number.
        * message body/text from the event payload.
        """
        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        text = str(event.payload.get("text", event.payload.get("body", "")))

        content[KEY_ID] = str(native_data.get("packet_id", ""))
        _longname, _shortname = self._resolve_mmrelay_sender_names(native_data)
        content[KEY_LONGNAME] = _longname
        content[KEY_SHORTNAME] = _shortname
        content[KEY_MESHNET] = self._resolve_mmrelay_meshnet(
            event, ctx_source_origin_label
        )
        content[KEY_PORTNUM] = PORTNUM_TEXT
        content[KEY_TEXT] = text
