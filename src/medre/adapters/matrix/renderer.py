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

from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.core.events import CanonicalEvent, EventRelation
from medre.core.rendering.attribution import (
    extract_relay_attribution,
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
    ) -> None:
        self._source_configs: dict[str, Any] = dict(source_configs or {})

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

    def _get_meshnet_name(self, event: CanonicalEvent) -> str:
        """Resolve meshnet_name for *event*'s source adapter.

        Returns the config's ``meshnet_name`` when a source config is
        matched; otherwise returns an empty string (neutral default).
        """
        cfg = self._resolve_source_config(event)
        if cfg is not None:
            return getattr(cfg, "meshnet_name", "")
        return ""

    def _get_matrix_relay_prefix(self, event: CanonicalEvent) -> str:
        """Resolve matrix_relay_prefix for *event*'s source adapter.

        Returns the config's ``matrix_relay_prefix`` when a source config
        is matched; otherwise returns an empty string (neutral default).
        """
        cfg = self._resolve_source_config(event)
        if cfg is not None:
            return getattr(cfg, "matrix_relay_prefix", "")
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

    def _detect_source_platform(self, event: CanonicalEvent) -> str | None:
        """Best-effort source platform detection for relay attribution.

        First inspects ``event.source_adapter`` for known platform
        fragments (mirrors the core heuristic).  When the adapter name
        does not contain a recognisable fragment, falls back to inspecting
        native metadata keys to identify the originating platform.
        """
        lowered = event.source_adapter.lower()
        for fragment, platform in (
            ("matrix", "matrix"),
            ("meshtastic", "meshtastic"),
            ("meshcore", "meshcore"),
            ("lxmf", "lxmf"),
        ):
            if fragment in lowered:
                return platform
        # Fallback: detect from native metadata keys.
        if event.metadata and event.metadata.native:
            data = event.metadata.native.data
            if any(k in data for k in ("longname", "shortname", "from_id")):
                return "meshtastic"
            if "pubkey_prefix" in data:
                return "meshcore"
            if "source_hash" in data:
                return "lxmf"
        return None

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

        # Apply relay prefix for mesh→Matrix direction
        body, prefix_meta = self._apply_matrix_relay_prefix(event, body)

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
                self._render_reaction(rel, content, target_adapter, event)

        # Embed metadata envelope
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())

        # Determine if a reaction relation was rendered (emote fallback sets
        # its own MMRelay metadata, so general injection must be skipped to
        # avoid overwriting KEY_TEXT with the payload body).
        _is_reaction = (
            event.relations and event.relations[0].relation_type == "reaction"
        )

        # Inject mmrelay-compatible metadata when enabled (skip for
        # reactions — _render_reaction already handles all MMRelay keys).
        if self._get_mmrelay_compat(event) and not _is_reaction:
            self._inject_mmrelay_metadata(event, content)

        metadata: dict[str, object] = {
            "renderer": self.name,
        }
        metadata.update(prefix_meta)

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
        body, prefix_meta = self._apply_matrix_relay_prefix(event, degraded_text)

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
            self._inject_mmrelay_metadata(event, content)

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
    ) -> tuple[str, dict[str, object]]:
        """Format the configured relay prefix for a reaction emote body.

        Uses the shared core attribution extractor and safe prefix formatter.

        Returns a ``(prefix_str, formatter_meta)`` tuple.
        ``prefix_str`` may be empty when no prefix template is configured.
        ``formatter_meta`` contains diagnostic keys when a prefix was
        formatted, empty dict otherwise.
        """
        template = self._get_matrix_relay_prefix(event)
        if not template:
            return "", {}

        meshnet_name = self._get_meshnet_name(event) or None
        source_platform = self._detect_source_platform(event)
        attr = extract_relay_attribution(
            event,
            source_platform=source_platform,
            source_meshnet_name=meshnet_name,
        )
        fmt_result = format_relay_prefix(template, attr)

        # On internal exception, return empty prefix (preserve safety).
        if fmt_result.formatting_error and fmt_result.formatting_error.startswith(
            "formatting_exception:"
        ):
            return "", {}

        formatter_meta: dict[str, object] = {
            "prefix_formatter": {
                "template_used": fmt_result.template_used,
                "variables_used": fmt_result.variables_used,
                "missing_variables": fmt_result.missing_variables,
                "unknown_variables": fmt_result.unknown_variables,
                "formatting_error": fmt_result.formatting_error,
            },
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
    ) -> None:
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
        else:
            # mmrelay_compat or missing Matrix-native target → m.emote fallback
            symbol = self._extract_reaction_symbol(rel, event)
            original_text = self._abbreviate_text(
                self._extract_original_text(rel, event)
            )
            prefix, _reaction_prefix_meta = self._format_reaction_prefix(event)

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
            content[KEY_LONGNAME] = str(native_data.get("longname", ""))
            content[KEY_SHORTNAME] = str(native_data.get("shortname", ""))
            content[KEY_MESHNET] = self._get_meshnet_name(event)
            content[KEY_PORTNUM] = PORTNUM_TEXT

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
    ) -> tuple[str, dict[str, object]]:
        """Prepend the configured relay prefix template to *body*.

        Uses the shared core attribution extractor and safe prefix formatter
        from :mod:`medre.core.rendering.attribution`.  Variables available
        in templates include all canonical ``source_*`` fields plus aliases
        (``longname``, ``shortname``, ``shortname5``, ``from_id``,
        ``meshnet_name``).

        Returns a ``(prefixed_body, formatter_meta)`` tuple.
        ``formatter_meta`` is empty when no prefix is configured or a
        formatting exception occurred; otherwise it contains diagnostic
        keys from :class:`PrefixFormatterResult`.
        """
        template = self._get_matrix_relay_prefix(event)
        if not template:
            return body, {}

        meshnet_name = self._get_meshnet_name(event) or None
        source_platform = self._detect_source_platform(event)
        attr = extract_relay_attribution(
            event,
            source_platform=source_platform,
            source_meshnet_name=meshnet_name,
        )
        fmt_result = format_relay_prefix(template, attr)

        # On internal exception, return body unchanged (preserve safety).
        if fmt_result.formatting_error and fmt_result.formatting_error.startswith(
            "formatting_exception:"
        ):
            return body, {}

        formatter_meta: dict[str, object] = {
            "prefix_formatter": {
                "template_used": fmt_result.template_used,
                "variables_used": fmt_result.variables_used,
                "missing_variables": fmt_result.missing_variables,
                "unknown_variables": fmt_result.unknown_variables,
                "formatting_error": fmt_result.formatting_error,
            },
        }
        return f"{fmt_result.rendered_prefix}{body}", formatter_meta

    # ------------------------------------------------------------------
    # mmrelay compatibility
    # ------------------------------------------------------------------

    def _inject_mmrelay_metadata(
        self,
        event: CanonicalEvent,
        content: dict[str, object],
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
        content[KEY_LONGNAME] = str(native_data.get("longname", ""))
        content[KEY_SHORTNAME] = str(native_data.get("shortname", ""))
        content[KEY_MESHNET] = self._get_meshnet_name(event)
        content[KEY_PORTNUM] = PORTNUM_TEXT
        content[KEY_TEXT] = text
