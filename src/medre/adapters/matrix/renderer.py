"""Matrix renderer for target-specific event rendering.

The :class:`MatrixRenderer` converts canonical events into Matrix-ready
content payloads (``m.room.message`` dicts with ``msgtype``, ``body``,
optional ``m.relates_to``, and a MEDRE metadata envelope).

This renderer is owned by the Matrix adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"matrix"``, the renderer
matches on that platform string directly.

**Supported relation types**: text messages, native replies, native
threads, native edits (``m.replace``), and reactions (true
``m.reaction`` or MMRelay emote fallback).  Native deletes render as
``redact_event`` operations.

Every native render emits a closed
:class:`~medre.adapters.matrix.outbound.MatrixOutboundOperation`
envelope under the single ``_matrix_operation`` payload key;
``MatrixAdapter.deliver`` pops and dispatches it before transport.

**Mutation authorization**: edits and deletes render natively only when
the relation's core-computed ``target_fact.status == "bound_owned"``.
A mutation that reaches native rendering without that fact raises
:class:`MatrixNativeMutationError` (renderer-level fail-close) — it
never degrades into an ordinary message.
"""

from __future__ import annotations

from typing import Any, Mapping

from medre.adapters._attribution_dispatch import project_source_fields
from medre.adapters._native_metadata_dispatch import current_native_namespace
from medre.adapters.matrix.event_shape import mmrelay_interop_fields
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.outbound import MatrixOutboundOperation
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


class MatrixNativeMutationError(RuntimeError):
    """Fail-close signal: an unauthorized native mutation reached rendering.

    Raised by the Matrix renderer when an edit or delete arrives in
    native mode without a ``bound_owned`` relation target fact.  The
    core delivery gate should have suppressed the delivery before
    rendering; this render-time guard is defense-in-depth so a missing
    suppression can never degrade a mutation into an ordinary message
    (which would fabricate content) or an unauthorized redaction.
    """


def _find_relation(
    relations: tuple[EventRelation, ...],
    relation_type: str,
) -> EventRelation | None:
    """Return the first relation of *relation_type*, or ``None``.

    Relation tuple order is incidental; scanning keeps thread+reply and
    edit+reply rendering order-independent.
    """
    for rel in relations:
        if rel.relation_type == relation_type:
            return rel
    return None


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
        """Resolve MMRelay sender names from current transport metadata.

        Meshtastic-originated canonical events use the versioned
        ``native.meshtastic`` namespace.  Explicit MMRelay wire fields under
        ``native.interop.mmrelay`` remain the fallback for externally encoded
        MMRelay events.  Abandoned root/dotted MEDRE fields are not read.
        """
        meshtastic = current_native_namespace(native_data, "meshtastic")
        interop = mmrelay_interop_fields(native_data)
        longname = meshtastic.get("longname") or interop.get(KEY_LONGNAME) or ""
        shortname = meshtastic.get("shortname") or interop.get(KEY_SHORTNAME) or ""
        return str(longname), str(shortname)

    @staticmethod
    def _resolve_mmrelay_packet_id(native_data: dict[str, object]) -> str:
        """Resolve the Meshtastic packet ID used by the MMRelay wire contract."""
        meshtastic = current_native_namespace(native_data, "meshtastic")
        interop = mmrelay_interop_fields(native_data)
        packet_id = meshtastic.get("packet_id")
        if packet_id is None:
            packet_id = interop.get(KEY_ID)
        return str(packet_id) if packet_id is not None else ""

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
        text within the Matrix payload body.  Native ``m.relates_to``
        fields are **not** emitted.  The body is produced using the same
        deterministic wording as
        :class:`~medre.core.rendering.text.TextRenderer` so that relation
        information is preserved as readable text.  The result carries
        ``fallback_applied="strategy_fallback_text"``.

        **Native / direct mode** — replies preserve ``m.in_reply_to``
        and inject ``KEY_REPLY_ID`` from native/relation metadata when
        available.  Reactions render as true ``m.reaction`` events when
        a target event/native Matrix id is available and mmrelay_compat
        is false.  When mmrelay_compat is true or the target is missing,
        an ``m.emote`` fallback is rendered with MMRelay keys.

        Threads render as native ``m.thread`` events rooted at the bound
        destination thread root.  Edits render ``m.replace`` /
        ``m.new_content`` events; deletes render ``redact_event``
        operations (mutation-authorized facts only — see
        :class:`MatrixNativeMutationError`).

        Every native render wraps its wire content in a closed
        ``send_event`` (or ``redact_event``) operation envelope under
        the ``_matrix_operation`` payload key.

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
        relations = event.relations
        edit_rel = _find_relation(relations, "edit")
        delete_rel = _find_relation(relations, "delete")
        thread_rel = _find_relation(relations, "thread")
        reply_rel = _find_relation(relations, "reply")

        # Mutations are fail-closed operations: they either render as
        # authorized native mutations or raise — never ordinary messages.
        if edit_rel is not None:
            return self._render_edit(event, ctx, edit_rel)
        if delete_rel is not None:
            return self._render_delete(event, ctx, delete_rel)

        # Thread rendering with reply-fallback parent semantics.  An
        # unbound root degrades to a plain message (honest degradation,
        # matching reply behavior), handled inside _render_thread.
        if thread_rel is not None:
            return self._render_thread(event, ctx, thread_rel, reply_rel)

        body = str(event.payload.get("text", event.payload.get("body", "")))

        # Determine if a reaction relation is present before applying the
        # body-level prefix — reactions manage their own prefix metadata.
        _is_reaction = bool(relations) and relations[0].relation_type == "reaction"

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
        event_type = "m.room.message"

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
                    mmrelay_id = mmrelay_interop_fields(native_data).get(KEY_REPLY_ID)
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
                reaction_prefix_meta, event_type = self._render_reaction(
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
            "matrix_operation": "send_event",
        }
        metadata.update(prefix_meta)
        metadata.update(reaction_prefix_meta)
        self._note_rendered_text(metadata, content)

        operation = MatrixOutboundOperation.send_event(event_type, content)

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=operation.to_payload(),
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
        envelope) without native ``m.relates_to`` fields.  Relation
        semantics are expressed as deterministic plain text in the
        ``body`` using the same wording as
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
            "matrix_operation": "send_event",
        }
        result_metadata.update(prefix_meta)
        self._note_rendered_text(result_metadata, content)
        if truncated:
            result_metadata["original_length"] = original_length
            result_metadata["original_text_bytes"] = original_text_bytes
            result_metadata["rendered_text_bytes"] = rendered_text_bytes
            if ctx.max_text_bytes is not None:
                result_metadata["max_text_bytes"] = ctx.max_text_bytes

        operation = MatrixOutboundOperation.send_event("m.room.message", content)

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload=operation.to_payload(),
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
        3. Event native metadata ``native.interop.mmrelay.meshtastic_text``
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
            text = mmrelay_interop_fields(native_data).get(KEY_TEXT)
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
    ) -> tuple[dict[str, object], str]:
        """Render a reaction relation into the Matrix content dict.

        When a Matrix-native target ID (owned by *target_adapter*) is
        available and mmrelay_compat is false, produces a true
        ``m.reaction`` event (the caller emits it with event type
        ``"m.reaction"``).

        When mmrelay_compat is true or no Matrix-native target exists,
        falls back to an ``m.emote`` ``m.room.message`` with
        MMRelay-compatible body and full mesh metadata.

        The canonical ``rel.target_event_id`` is **never** used as a
        Matrix event ID — it is an internal MEDRE canonical ID.

        Returns a ``(reaction_prefix_meta, event_type)`` tuple.
        ``reaction_prefix_meta`` is empty for true ``m.reaction``
        annotations (no prefix metadata applies) and carries prefix
        diagnostics for the emote fallback.
        """
        mx_event_id = self._matrix_target_event_id(rel, target_adapter)

        # Extract MMRelay reply ID from relation metadata for fallback
        rel_meta = getattr(rel, "metadata", {}) or {}

        if mx_event_id is not None and not self._get_mmrelay_compat(event):
            # True Matrix reaction — emitted as an m.reaction event
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
            # True m.reaction carries no prefix metadata — body is removed.
            return {}, "m.reaction"
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

            content[KEY_ID] = self._resolve_mmrelay_packet_id(native_data)
            _longname, _shortname = self._resolve_mmrelay_sender_names(native_data)
            content[KEY_LONGNAME] = _longname
            content[KEY_SHORTNAME] = _shortname
            content[KEY_MESHNET] = self._resolve_mmrelay_meshnet(
                event,
                ctx.source_origin_label if ctx is not None else None,
            )
            content[KEY_PORTNUM] = PORTNUM_TEXT

            return _reaction_prefix_meta, "m.room.message"

    # ------------------------------------------------------------------
    # Native thread / edit / delete rendering
    # ------------------------------------------------------------------

    @staticmethod
    def _bound_native_target(
        rel: EventRelation | None, target_adapter: str
    ) -> str | None:
        """Return the bound destination native ID for a referential relation.

        Referential authority stays with the stored ``target_native_ref``
        (adapter-scoped); a destination-scoped ``target_fact`` from core
        binding is honored when it confirms the same destination.  Source
        platform IDs and canonical event IDs are never returned.
        """
        if rel is None:
            return None
        ref_id = MatrixRenderer._matrix_target_event_id(rel, target_adapter)
        if ref_id:
            return ref_id
        fact = getattr(rel, "target_fact", None)
        if (
            fact is not None
            and getattr(fact, "status", None) in ("bound", "bound_owned")
            and getattr(fact, "adapter", None) == target_adapter
            and getattr(fact, "native_message_id", None)
        ):
            return str(fact.native_message_id)
        return None

    @staticmethod
    def _require_bound_owned_target(rel: EventRelation, target_adapter: str) -> str:
        """Return the mutation target native ID, enforcing ``bound_owned``.

        Raises :class:`MatrixNativeMutationError` when the relation's
        core-computed ``target_fact`` is missing, not ``bound_owned``, or
        carries no usable destination native ID.  This is the
        renderer-level fail-close: mutations never degrade into ordinary
        messages and never act on unproven targets.
        """
        fact = getattr(rel, "target_fact", None)
        if fact is None or getattr(fact, "status", None) != "bound_owned":
            raise MatrixNativeMutationError(
                "native mutation refused: relation target_fact is not "
                f"bound_owned (status={getattr(fact, 'status', None)!r})"
            )
        fact_adapter = getattr(fact, "adapter", None)
        fact_id = getattr(fact, "native_message_id", None)
        if fact_id and fact_adapter == target_adapter:
            return str(fact_id)
        # bound_owned but no usable destination-scoped id — fail closed.
        raise MatrixNativeMutationError(
            "native mutation refused: bound_owned fact carries no "
            f"destination native_message_id for adapter {target_adapter!r}"
        )

    def _render_thread(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
        thread_rel: EventRelation,
        reply_rel: EventRelation | None,
    ) -> RenderingResult:
        """Render a thread relation as a native ``m.thread`` event.

        The thread root must be a bound destination native ID; an
        unbound root degrades honestly to a plain message without
        ``m.relates_to`` (never a fabricated source-platform ID).

        Parent selection: an explicit bound reply relation on the same
        event becomes ``m.in_reply_to`` with ``is_falling_back=false``;
        otherwise the root itself is the fallback parent with
        ``is_falling_back=true`` (spec fallback-parent semantics).
        Tuple order of thread/reply relations is incidental.
        """
        target_adapter = ctx.target_adapter
        root_id = self._bound_native_target(thread_rel, target_adapter)

        if not root_id:
            # Unbound root — render a plain message without m.relates_to.
            return self._render_plain_message(event, ctx)

        parent_id = self._bound_native_target(reply_rel, target_adapter)
        if parent_id:
            relates_to: dict[str, object] = {
                "rel_type": "m.thread",
                "event_id": root_id,
                "is_falling_back": False,
                "m.in_reply_to": {"event_id": parent_id},
            }
        else:
            relates_to = {
                "rel_type": "m.thread",
                "event_id": root_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": root_id},
            }

        body = str(event.payload.get("text", event.payload.get("body", "")))
        body, prefix_meta = self._apply_matrix_relay_prefix(
            event, body, target_adapter, ctx
        )

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(body),
            "m.relates_to": relates_to,
        }

        metadata = self._finalize_send_content(event, ctx, content)

        metadata.update(prefix_meta)
        metadata["matrix_thread_root"] = root_id
        if parent_id:
            metadata["matrix_thread_parent"] = parent_id

        operation = MatrixOutboundOperation.send_event("m.room.message", content)
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=ctx.target_channel,
            payload=operation.to_payload(),
            metadata=metadata,
            fallback_applied=None,
        )

    def _render_plain_message(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render a plain ``m.room.message`` with no relation metadata."""
        target_adapter = ctx.target_adapter
        body = str(event.payload.get("text", event.payload.get("body", "")))
        body, prefix_meta = self._apply_matrix_relay_prefix(
            event, body, target_adapter, ctx
        )

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(body),
        }

        metadata = self._finalize_send_content(event, ctx, content)
        metadata.update(prefix_meta)

        operation = MatrixOutboundOperation.send_event("m.room.message", content)
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=ctx.target_channel,
            payload=operation.to_payload(),
            metadata=metadata,
            fallback_applied=None,
        )

    def _render_edit(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
        edit_rel: EventRelation,
    ) -> RenderingResult:
        """Render an edit relation as a native ``m.replace`` event.

        Wire shape (spec event replacements):

        * top-level ``body`` is the ``"* "`` fallback for clients that
          do not understand replacements;
        * ``m.new_content`` carries the new msgtype/body/format/
          formatted_body with exactly ONE relay attribution applied to
          the new body;
        * ``m.relates_to`` is ``{"rel_type": "m.replace", "event_id":
          <bound ORIGINAL copy native id>}``.

        Bound reply/thread relations carried by the edit event itself
        are mirrored into ``m.new_content["m.relates_to"]`` so the
        source platform's asserted relation metadata survives the edit.

        Requires a ``bound_owned`` target fact (renderer fail-close).
        Text only — binary attachments are unsupported and unchanged.
        """
        target_adapter = ctx.target_adapter
        original_id = self._require_bound_owned_target(edit_rel, target_adapter)

        new_text = str(event.payload.get("text", event.payload.get("body", "")))
        # Exactly one relay attribution: applied once to the new body.
        new_body, prefix_meta = self._apply_matrix_relay_prefix(
            event, new_text, target_adapter, ctx
        )
        fallback_body = f"* {new_body}"

        new_content: dict[str, object] = {
            "msgtype": "m.text",
            "body": new_body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(new_body),
        }
        mirrored = self._mirror_edit_relations(event, edit_rel, target_adapter)
        if mirrored is not None:
            new_content["m.relates_to"] = mirrored

        content: dict[str, object] = {
            "msgtype": "m.text",
            "body": fallback_body,
            "format": "org.matrix.custom.html",
            "formatted_body": self._text_to_html(fallback_body),
            "m.new_content": new_content,
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": original_id,
            },
        }

        metadata = self._finalize_send_content(event, ctx, content)
        metadata.update(prefix_meta)
        metadata["matrix_edit_target"] = original_id

        operation = MatrixOutboundOperation.send_event("m.room.message", content)
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=ctx.target_channel,
            payload=operation.to_payload(),
            metadata=metadata,
            fallback_applied=None,
        )

    def _mirror_edit_relations(
        self,
        event: CanonicalEvent,
        edit_rel: EventRelation,
        target_adapter: str,
    ) -> dict[str, object] | None:
        """Mirror bound reply/thread relations into an edit's new content.

        An edit event that itself carries bound reply/thread relations
        (its own semantics on the source platform) keeps them inside
        ``m.new_content["m.relates_to"]``.  Only destination-native IDs
        are used; unbound relations are omitted (never replaced with
        canonical or source-platform identifiers).
        """
        thread_rel = _find_relation(event.relations, "thread")
        reply_rel = _find_relation(event.relations, "reply")

        root_id = (
            self._bound_native_target(thread_rel, target_adapter)
            if thread_rel is not None
            else None
        )
        parent_id = (
            self._bound_native_target(reply_rel, target_adapter)
            if reply_rel is not None
            else None
        )

        if root_id:
            if parent_id:
                return {
                    "rel_type": "m.thread",
                    "event_id": root_id,
                    "is_falling_back": False,
                    "m.in_reply_to": {"event_id": parent_id},
                }
            return {
                "rel_type": "m.thread",
                "event_id": root_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": root_id},
            }
        if parent_id:
            return {"m.in_reply_to": {"event_id": parent_id}}
        return None

    def _render_delete(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
        delete_rel: EventRelation,
    ) -> RenderingResult:
        """Render a delete relation as a ``redact_event`` operation.

        Only ``bound_owned`` targets are redacted (renderer-level
        fail-close; the core delivery gate suppresses everything else
        before rendering).  The reason is neutral; no content envelope
        and no fabricated body are emitted.
        """
        target_adapter = ctx.target_adapter
        target_id = self._require_bound_owned_target(delete_rel, target_adapter)

        operation = MatrixOutboundOperation.redact(target_id)

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=ctx.target_channel,
            payload=operation.to_payload(),
            metadata={
                "renderer": self.name,
                "matrix_operation": "redact_event",
                "matrix_redacts_event_id": target_id,
            },
            fallback_applied=None,
        )

    def _finalize_send_content(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
        content: dict[str, object],
    ) -> dict[str, object]:
        """Attach provenance envelope and optional MMRelay metadata.

        Shared by the thread/edit/plain native renders.  Returns the
        result-metadata dict (renderer name + operation kind).
        """
        envelope = MatrixMetadataEnvelope(
            canonical_event_id=event.event_id,
            source_adapter=event.source_adapter,
            source_channel=event.source_channel_id or "",
            metadata_mode="safe",
        )
        content.update(envelope.to_content())
        if self._get_mmrelay_compat(event):
            self._inject_mmrelay_metadata(event, content, ctx.source_origin_label)
        metadata: dict[str, object] = {
            "renderer": self.name,
            "matrix_operation": "send_event",
        }
        self._note_rendered_text(metadata, content)
        return metadata

    @staticmethod
    def _note_rendered_text(
        metadata: dict[str, object],
        content: dict[str, object],
    ) -> None:
        """Record rendered-text metrics for delivery evidence.

        The closed outbound envelope moves wire content off the payload
        top level, so the evidence extractor's payload-key fallback can no
        longer see the rendered body; renderer metadata is the supported
        channel for these metrics.  True ``m.reaction`` annotations carry
        no body and stay metric-free.
        """
        body = content.get("body")
        if not isinstance(body, str):
            return
        metadata.setdefault("rendered_text_chars", len(body))
        metadata.setdefault("rendered_text_bytes", len(body.encode("utf-8")))

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

        # Normalize CRLF and CR line endings to LF before escaping.
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
        * mesh network name derived from origin-label logic.
        * hardcoded ``"TEXT_MESSAGE_APP"`` port number.
        * message body/text from the event payload.
        """
        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        text = str(event.payload.get("text", event.payload.get("body", "")))

        content[KEY_ID] = self._resolve_mmrelay_packet_id(native_data)
        _longname, _shortname = self._resolve_mmrelay_sender_names(native_data)
        content[KEY_LONGNAME] = _longname
        content[KEY_SHORTNAME] = _shortname
        content[KEY_MESHNET] = self._resolve_mmrelay_meshnet(
            event, ctx_source_origin_label
        )
        content[KEY_PORTNUM] = PORTNUM_TEXT
        content[KEY_TEXT] = text


def build_matrix_renderer(
    *,
    runtime_configs: Mapping[str, Any],
    all_runtime_configs: Mapping[str, Mapping[str, Any]],
    source_attribution: Mapping[str, Any],
) -> MatrixRenderer | None:
    """Build the registered Matrix renderer for runtime assembly."""
    from medre.config.adapters.matrix import MatrixConfig

    configs: dict[str, MatrixConfig] = {}
    for rtc in runtime_configs.values():
        if not getattr(rtc, "enabled", False):
            continue
        adapter_id = rtc.adapter_id
        config = rtc.config
        if config is None:
            config = MatrixConfig(adapter_id=adapter_id, homeserver="", user_id="")
        configs[adapter_id] = config
    if not configs:
        return None

    source_configs: dict[str, Any] = {}
    for rtc in all_runtime_configs.get("meshtastic", {}).values():
        if not getattr(rtc, "enabled", False):
            continue
        config = getattr(rtc, "config", None)
        if config is not None:
            source_configs[rtc.adapter_id] = config

    return MatrixRenderer(
        source_configs=source_configs,
        source_attribution=dict(source_attribution),
        configs=configs,
    )
