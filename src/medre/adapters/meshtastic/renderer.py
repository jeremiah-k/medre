"""Meshtastic renderer for target-specific event rendering.

The :class:`MeshtasticRenderer` converts canonical events into
Meshtastic-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

The renderer is initialised with a **required** mapping of adapter IDs to
:class:`~medre.config.adapters.meshtastic.MeshtasticConfig` instances.
At render time the config for *target_adapter* is resolved from this
mapping — there is no fallback or default.  An empty mapping raises
:class:`ValueError` at construction; an unknown *target_adapter* raises
:class:`KeyError` at render time.

When the resolved config contains a non-empty ``radio_relay_prefix``,
the renderer prepends a formatted prefix to the message text.  The prefix
template uses Python ``str.format()`` syntax with the following variables:

* ``{longname}`` — sender long name (from event native metadata, if available).
* ``{shortname}`` — sender short name (from event native metadata, if available).
* ``{shortname5}`` — first 5 characters of ``{shortname}`` (or ``{from_id}``
  if shortname is empty).
* ``{meshnet_name}`` — the mesh network name from the adapter config.
* ``{from_id}`` — the sender's numeric node ID.

This renderer is owned by the Meshtastic adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshtastic"``, the renderer
matches on that platform string directly.

Text messages, replies, and reactions are supported.  UTF-8 byte-budget
truncation is applied after final radio text rendering (including prefix,
reply, and reaction formatting).  Multi-byte UTF-8 codepoints are never
split.  Truncation metadata and the ``RenderingResult.truncated`` flag
report whether the text was trimmed.

**Cross-platform reaction rendering (MMRelay-compatible).**
When a reaction originates from a *different* adapter than the Meshtastic
target (e.g. Matrix → Meshtastic), the renderer emits descriptive text
rather than a native Meshtastic emoji tapback:

* ``{compact_prefix} reacted {emoji} to "{abbreviated_text}"``
* Spaces are stripped from the display-name tokens in the prefix to
  conserve meshnet bandwidth; casing is preserved.  A separator
  space is inserted between the prefix and ``reacted`` only when the
  prefix is non-empty.
* ``reply_id`` is set when the original message has a known Meshtastic
  packet ID (via ``target_native_ref`` or ``meshtastic_reply_id`` metadata),
  allowing the descriptive text to be sent as a structured reply.
* ``emoji`` is **not** set to ``1`` — the payload is plain text, not a
  native tapback.

Native Meshtastic-originated reactions continue to use ``emoji=1`` +
``reply_id`` for proper tapback round-tripping.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Mapping

from medre.core.events import CanonicalEvent, EventKind, EventRelation
from medre.core.rendering.attribution import (
    PrefixFormatterResult,
    extract_relay_attribution,
    format_relay_prefix,
)
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingResult,
)

if TYPE_CHECKING:
    from medre.config.adapters.meshtastic import MeshtasticConfig


class MeshtasticRenderer:
    """Renderer for Meshtastic transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    **Target-aware rendering.** The renderer is initialised with a
    mapping of adapter IDs to :class:`~medre.config.adapters.meshtastic.MeshtasticConfig`
    instances.  At render time the config for *target_adapter* is resolved
    from this mapping.  This allows multi-radio setups where each adapter
    has different ``max_text_bytes``, ``radio_relay_prefix``, and
    ``meshnet_name`` values.

    An empty *configs* mapping raises :class:`ValueError`.  An unknown
    *target_adapter* at render time raises :class:`KeyError`.

    Selection is via the pipeline's platform registry.
    """

    name: str = "meshtastic"
    """Renderer identifier used by the rendering pipeline for platform
    registry matching (``ctx.target_platform`` comparison)."""

    _PLATFORM: str = "meshtastic"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(
        self,
        *,
        configs: Mapping[str, MeshtasticConfig],
    ) -> None:
        if not configs:
            raise ValueError(
                "MeshtasticRenderer requires at least one adapter config. "
                "Pass a non-empty configs mapping."
            )
        self._configs: dict[str, MeshtasticConfig] = dict(configs)

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    # Event kinds that have a natural plain-text representation for
    # Meshtastic radio transports.
    _SUPPORTED_KINDS: frozenset[str] = frozenset(
        {
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.MESSAGE_EDITED,
            EventKind.MESSAGE_DELETED,
            EventKind.MESSAGE_REACTED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        }
    )

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` when *ctx.target_platform* is ``"meshtastic"``
        and the event kind is supported.

        Parameters
        ----------
        event:
            The canonical event to check.
        ctx:
            Frozen rendering context with target identity and delivery
            metadata.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        return (
            ctx.target_platform == self._PLATFORM
            and event.event_kind in self._SUPPORTED_KINDS
        )

    # ------------------------------------------------------------------
    # Prefix formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_prefix_for(
        event: CanonicalEvent,
        radio_relay_prefix: str,
        meshnet_name: str,
        *,
        compact: bool = False,
    ) -> PrefixFormatterResult:
        """Format a prefix template using shared attribution extraction.

        Delegates to :func:`~medre.core.rendering.attribution.extract_relay_attribution`
        for data extraction and :func:`~medre.core.rendering.attribution.format_relay_prefix`
        for safe template rendering.  Falls back to reading flat
        Meshtastic-style keys (``longname``, ``shortname``, ``from_id``)
        directly from native metadata when the platform-specific extractor
        does not populate them.

        Available template variables (and their aliases):

        * ``{longname}`` — sender long name.
        * ``{shortname}`` — sender short name.
        * ``{shortname5}`` — first 5 chars of shortname (or from_id).
        * ``{meshnet_name}`` — mesh network name from adapter config.
        * ``{from_id}`` — sender node ID.
        * Plus all canonical ``source_*`` fields from :class:`RelayAttribution`.

        Falls back to empty strings for any unavailable variables.
        Never renders the literal text ``"None"``.

        When *compact* is ``True``, spaces are stripped from display-name
        tokens (longname, shortname) before template substitution.

        Parameters
        ----------
        event:
            The canonical event whose source metadata is used for formatting.
        radio_relay_prefix:
            The prefix template string.
        meshnet_name:
            The mesh network name for ``{meshnet_name}`` substitution.
        compact:
            When ``True``, strip spaces from display-name tokens.

        Returns
        -------
        PrefixFormatterResult
            Frozen result with rendered prefix string and diagnostic
            metadata.
        """
        if not radio_relay_prefix:
            return PrefixFormatterResult(
                rendered_prefix="",
                template_used=radio_relay_prefix,
                variables_used=(),
                missing_variables=(),
                unknown_variables=(),
                formatting_error=None,
            )

        attr = extract_relay_attribution(
            event,
            source_meshnet_name=meshnet_name or None,
        )

        # Flat-key fallback: the codec pipeline may store
        # Meshtastic-style flat keys (longname, shortname, from_id)
        # in native_data regardless of source platform.  Patch any
        # attribution fields that the platform extractor left empty.
        native_data: dict[str, object] = {}
        if event.metadata and event.metadata.native:
            native_data = dict(event.metadata.native.data)

        patches: dict[str, str | None] = {}
        if not attr.source_long_name:
            ln = native_data.get("longname")
            patches["source_long_name"] = str(ln) if ln is not None else None
        if not attr.source_short_name:
            sn = native_data.get("shortname")
            patches["source_short_name"] = str(sn) if sn is not None else None
        if not attr.source_sender_id:
            fid = native_data.get("from_id", event.source_transport_id)
            patches["source_sender_id"] = str(fid) if fid is not None else None
        if patches.get("source_long_name") and not attr.source_display_name:
            patches["source_display_name"] = patches["source_long_name"]

        if patches:
            # Clear short_name_5 so it is re-derived from patched values.
            if "source_short_name" in patches or "source_sender_id" in patches:
                patches["source_short_name_5"] = None
            attr = replace(attr, **patches)

        if compact:
            long_compact = (attr.source_long_name or "").replace(" ", "") or None
            short_compact = (attr.source_short_name or "").replace(" ", "") or None
            display_compact = (attr.source_display_name or "").replace(" ", "") or None
            attr = replace(
                attr,
                source_display_name=display_compact,
                source_long_name=long_compact,
                source_short_name=short_compact,
                source_short_name_5=None,  # Force re-derivation
            )

        return format_relay_prefix(radio_relay_prefix, attr)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render a canonical event into a Meshtastic content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload, with the
          configured ``radio_relay_prefix`` prepended if set.
        * ``channel_index``: the adapter's ``default_channel``, overridden
          only when *ctx.target_channel* is a valid numeric value.
        * ``meshnet_name``: the configured mesh network name.

        **Target-aware config resolution.** The renderer resolves the
        config for *ctx.target_adapter* from the ``configs`` mapping
        supplied at construction.  If the adapter is not found, a
        :class:`KeyError` is raised — there is no fallback.

        **Fallback-text mode** — when ``ctx.delivery_strategy`` is
        ``"fallback_text"``, relation semantics are degraded into plain
        text within the native Meshtastic payload.  Native relation
        fields (``reply_id``, ``emoji``) are suppressed; relation
        context is expressed as readable text instead.  All Meshtastic
        payload ownership is preserved: ``channel_index``,
        ``meshnet_name``, prefix rules, and UTF-8 byte-safe truncation.

        **Direct mode** — when ``ctx.delivery_strategy`` is ``"direct"``
        (or any non-fallback strategy), the renderer uses native
        Meshtastic relation fields:

        * *reply* with a numeric ``target_native_ref.native_message_id`` —
          sets ``reply_id`` (int) and emits plain text without fallback
          prefix.
        * *reply* without numeric native ref — emits plain text
          without ``reply_id``; no fallback prefix is added.
        * *native reaction* (same adapter) with a numeric
          ``target_native_ref.native_message_id`` — sets ``reply_id``
          (int) and ``emoji`` (1); text is the emoji string from
          ``relation.key`` or payload ``key``/``body``.
        * *native reaction* without numeric native ref — emits readable
          fallback text ``"[reacted: {emoji}]"`` with no ``emoji`` field.
        * *cross-platform reaction* (different source adapter, e.g.
          Matrix → Meshtastic) — emits MMRelay-compatible descriptive
          text: ``{compact_prefix} reacted {emoji} to "{abbreviated}"``.
          Sets ``reply_id`` if a Meshtastic packet ID is available.
          Does **not** set ``emoji=1``.

        Cross-platform reaction previews are abbreviated to 40 characters.

        UTF-8 byte-budget truncation is applied after final radio text
        rendering (including prefix, reply, and reaction formatting).
        The byte budget defaults to 227 (``MeshtasticConfig.max_text_bytes``)
        and is configurable per adapter instance.  Note: the budget applies
        to text only; ``reply_id``/``emoji`` protobuf fields are encoded
        separately and consume up to ~8 bytes of the 233-byte wire limit.
        Operators sending relation-heavy traffic should consider lowering
        ``max_text_bytes`` to ~219-225.

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
            The rendered Meshtastic content dict wrapped in a result.
        """
        # Unpack context.
        target_adapter = ctx.target_adapter
        target_channel = ctx.target_channel
        delivery_strategy = ctx.delivery_strategy
        is_fallback = delivery_strategy == "fallback_text"

        # Resolve target-adapter-specific config.
        try:
            adapter_config = self._configs[target_adapter]
        except KeyError:
            raise KeyError(
                f"No MeshtasticConfig registered for target_adapter "
                f"{target_adapter!r}. Known adapters: "
                f"{sorted(self._configs.keys())}"
            ) from None

        prefix = adapter_config.radio_relay_prefix
        meshnet_name = adapter_config.meshnet_name
        max_text_bytes = (
            ctx.max_text_bytes
            if ctx.max_text_bytes is not None
            else adapter_config.max_text_bytes
        )

        # Resolve channel index: adapter config default, overridden only by
        # a valid numeric target_channel.  Invalid/non-numeric values keep
        # the adapter's default_channel (do not silently force 0).
        channel_index = adapter_config.default_channel
        if target_channel is not None:
            try:
                channel_index = int(target_channel)
            except (ValueError, TypeError):
                pass

        content: dict[str, object] = {
            "channel_index": channel_index,
            "meshnet_name": meshnet_name,
        }

        # -- Structured reply / reaction rendering ----------------------------
        is_structured_reaction = False
        is_descriptive_reaction = False

        if event.relations:
            rel = event.relations[0]

            if is_fallback:
                # -- fallback_text: degrade relations into plain text --------
                content["text"] = self._render_fallback_text(
                    event,
                    rel,
                    prefix,
                    meshnet_name,
                    target_adapter,
                )
                if rel.relation_type == "reaction" and not self._is_native_reaction(
                    event,
                    target_adapter,
                ):
                    is_descriptive_reaction = True
            else:
                # -- direct: native Meshtastic relation fields ---------------
                reply_id = self._meshtastic_reply_id_from_relation(
                    rel,
                    target_adapter,
                )

                if rel.relation_type == "reply":
                    # Reply: plain text, optionally with native reply_id.
                    content["text"] = self._plain_text(event)
                    if reply_id is not None:
                        content["reply_id"] = reply_id
                elif rel.relation_type == "reaction":
                    emoji_text = self._resolve_emoji_text(rel, event) or ""
                    if self._is_native_reaction(event, target_adapter):
                        # Native Meshtastic tapback
                        if reply_id is not None:
                            content["text"] = emoji_text
                            content["reply_id"] = reply_id
                            content["emoji"] = 1
                            is_structured_reaction = True
                        else:
                            content["text"] = f"[reacted: {emoji_text}]"
                    else:
                        # Cross-platform MMRelay-style descriptive reaction
                        orig_preview = self._abbreviated_original_text(event, rel)
                        compact_prefix_result = self._format_prefix_for(
                            event,
                            prefix,
                            meshnet_name,
                            compact=True,
                        )
                        compact_prefix = compact_prefix_result.rendered_prefix
                        sep = ""
                        if compact_prefix and not compact_prefix[-1:].isspace():
                            sep = " "
                        content["text"] = (
                            f"{compact_prefix}{sep}reacted {emoji_text} "
                            f'to "{orig_preview}"'
                        )
                        if reply_id is not None:
                            content["reply_id"] = reply_id
                        is_descriptive_reaction = True
                else:
                    content["text"] = self._extract_text(event)
        else:
            content["text"] = self._extract_text(event)

        # Prepend relay prefix when configured
        # (skip for native emoji-only reactions; descriptive reactions
        # already include their compact prefix in the text).
        prefix_result: PrefixFormatterResult = PrefixFormatterResult(
            rendered_prefix="",
            template_used="",
            variables_used=(),
            missing_variables=(),
            unknown_variables=(),
            formatting_error=None,
        )
        if not is_structured_reaction and not is_descriptive_reaction:
            prefix_result = self._format_prefix_for(
                event,
                prefix,
                meshnet_name,
            )
            if prefix_result.rendered_prefix:
                content["text"] = f"{prefix_result.rendered_prefix}{content['text']}"

        # -- UTF-8 byte-budget truncation after final rendering ------
        final_text = str(content.get("text", ""))
        truncated_text, was_truncated, original_bytes, rendered_bytes = (
            self._truncate_utf8_bytes(final_text, max_text_bytes)
        )
        content["text"] = truncated_text

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(final_text),
            "rendered_length": len(truncated_text),
            "original_text_bytes": original_bytes,
            "rendered_text_bytes": rendered_bytes,
            "max_text_bytes": max_text_bytes,
            "truncated": was_truncated,
        }
        formatted_prefix = prefix_result.rendered_prefix
        if formatted_prefix:
            metadata["radio_relay_prefix"] = formatted_prefix
            metadata["prefix_template_used"] = prefix_result.template_used
            metadata["prefix_variables_used"] = prefix_result.variables_used
            metadata["prefix_missing_variables"] = prefix_result.missing_variables
        if is_descriptive_reaction:
            metadata["descriptive_reaction"] = True
        if is_fallback:
            metadata["delivery_strategy"] = delivery_strategy

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            truncated=was_truncated,
            fallback_applied="strategy_fallback_text" if is_fallback else None,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _render_fallback_text(
        self,
        event: CanonicalEvent,
        rel: EventRelation,
        prefix: str,
        meshnet_name: str,
        target_adapter: str,
    ) -> str:
        """Render relation semantics as degraded text for fallback_text mode.

        In fallback_text mode, native relation fields (``reply_id``,
        ``emoji``) are suppressed.  Relation context is expressed as
        readable text instead, preserving Meshtastic payload ownership.

        Parameters
        ----------
        event:
            The canonical event being rendered.
        rel:
            The first relation on the event.
        prefix:
            Radio relay prefix template from adapter config.
        meshnet_name:
            Mesh network name from adapter config.
        target_adapter:
            The target adapter identifier.

        Returns
        -------
        str
            Text with relation semantics degraded into readable form.
        """
        if rel.relation_type == "reply":
            # Reply degraded to text: include "[replying to: …]" prefix.
            # When fallback_text is present, _extract_text handles it.
            # When absent, resolve a deterministic marker from target_event_id
            # or target_native_ref so relation context is never lost.
            if rel.fallback_text:
                return self._extract_text(event)
            target_marker = self._resolve_reply_target_marker(rel)
            if target_marker:
                body = str(event.payload.get("text", event.payload.get("body", "")))
                return f"[replying to: {target_marker}] {body}"
            return self._extract_text(event)

        if rel.relation_type == "reaction":
            emoji_text = self._resolve_emoji_text(rel, event) or ""
            if self._is_native_reaction(event, target_adapter):
                # Native reaction degraded to readable text (no tapback).
                return f"[reacted: {emoji_text}]"
            # Cross-platform reaction: MMRelay-style descriptive text,
            # but without native reply_id.
            orig_preview = self._abbreviated_original_text(event, rel)
            compact_prefix = self._format_prefix_for(
                event,
                prefix,
                meshnet_name,
                compact=True,
            ).rendered_prefix
            sep = ""
            if compact_prefix and not compact_prefix[-1:].isspace():
                sep = " "
            return f"{compact_prefix}{sep}reacted {emoji_text} " f'to "{orig_preview}"'

        if rel.relation_type == "edit":
            payload_text = str(event.payload.get("text", event.payload.get("body", "")))
            if payload_text:
                return f"[edited] {payload_text}"
            return "[edited]"

        if rel.relation_type == "delete":
            target_marker = self._resolve_reply_target_marker(rel)
            if target_marker:
                return f"[deleted: {target_marker}]"
            return "[deleted]"

        if rel.relation_type == "thread":
            payload_text = str(event.payload.get("text", event.payload.get("body", "")))
            target_marker = self._resolve_reply_target_marker(rel) or "?"
            if payload_text:
                return f"[thread: {target_marker}] {payload_text}"
            return f"[thread: {target_marker}]"

        # Unknown relation types: standard text extraction.
        return self._extract_text(event)

    @staticmethod
    def _resolve_reply_target_marker(rel: EventRelation) -> str | None:
        """Resolve a deterministic target identifier for a reply relation.

        Used by ``_render_fallback_text`` when ``rel.fallback_text`` is
        absent but the relation still carries enough context to identify
        the target message.

        Resolution order:
        1. ``rel.target_event_id`` (if present).
        2. ``rel.target_native_ref.native_message_id`` (if present).
        3. ``None`` — no usable identifier.
        """
        if rel.target_event_id is not None:
            return rel.target_event_id
        ref = rel.target_native_ref
        if ref is not None:
            mid = getattr(ref, "native_message_id", None)
            if mid is not None:
                return str(mid)
        return None

    @staticmethod
    def _truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool, int, int]:
        """Truncate *text* to at most *max_bytes* UTF-8 bytes.

        Follows the MMRelay conceptual pattern: encode the full text to
        UTF-8 bytes, slice the byte buffer to the configured budget, then
        decode back with ``errors="ignore"`` to avoid splitting multi-byte
        codepoints.  This is the same approach used by mmrelay's
        ``truncate_message_bytes`` utility.

        Parameters
        ----------
        text:
            The text to potentially truncate.
        max_bytes:
            Maximum number of UTF-8 bytes allowed.  Must be >= 0
            (validation is handled by ``MeshtasticConfig.validate()``).

        Returns
        -------
        tuple[str, bool, int, int]
            ``(truncated_text, was_truncated, original_byte_count,
            rendered_byte_count)``.
        """
        if max_bytes == 0:
            original = len(text.encode("utf-8"))
            return ("", original > 0, original, 0)

        encoded = text.encode("utf-8")
        original_bytes = len(encoded)

        if original_bytes <= max_bytes:
            return (text, False, original_bytes, original_bytes)

        # Slice to byte budget and decode with errors="ignore" to
        # avoid splitting multi-byte UTF-8 codepoints.
        truncated_bytes = encoded[:max_bytes]
        truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
        rendered_bytes = len(truncated_text.encode("utf-8"))

        return (truncated_text, True, original_bytes, rendered_bytes)

    @staticmethod
    def _is_native_reaction(event: CanonicalEvent, target_adapter: str) -> bool:
        """Return ``True`` when a reaction event originates from the same
        adapter as the Meshtastic target.

        Native Meshtastic tapbacks (``emoji=1``) are emitted only when
        the reaction was decoded from a Meshtastic packet.  Cross-platform
        reactions (e.g. Matrix → Meshtastic) use MMRelay-style descriptive
        text instead.

        Parameters
        ----------
        event:
            The canonical event being rendered.
        target_adapter:
            The adapter name of the Meshtastic target.

        Returns
        -------
        bool
        """
        return event.source_adapter == target_adapter

    @staticmethod
    def _abbreviated_original_text(
        event: CanonicalEvent, relation: EventRelation
    ) -> str:
        """Return an abbreviated preview of the original message being
        reacted to.

        Source preference order:

        1. ``relation.metadata["original_text"]`` — mapped original
           message text from the source codec.
        2. ``relation.fallback_text`` — human-readable fallback.
        3. ``event.payload`` ``body`` / ``text`` — event body as last
           resort.

        Newlines are normalised to spaces.  The result is truncated to
        40 characters with an ellipsis (``"..."``) when longer.

        Parameters
        ----------
        event:
            The canonical event (used for payload fallback).
        relation:
            The reaction relation whose target text is previewed.

        Returns
        -------
        str
            Abbreviated original-text preview.
        """
        # 1. Mapped original message metadata
        rel_meta = getattr(relation, "metadata", None) or {}
        source_text = rel_meta.get("original_text")

        # 2. Fallback text from relation
        if not source_text:
            source_text = relation.fallback_text

        # 3. Event payload body/text
        if not source_text:
            source_text = str(event.payload.get("text", event.payload.get("body", "")))

        # Normalise: treat str and non-str uniformly
        text = str(source_text) if source_text else ""

        # Strip quoted reply lines (lines starting with "> ") if present
        lines = text.split("\n")
        non_quoted = [ln for ln in lines if not ln.startswith("> ")]
        text = " ".join(non_quoted).strip()

        # Normalise remaining whitespace
        text = " ".join(text.split())

        # Abbreviate to 40 chars with "..."
        if len(text) > 40:
            text = text[:40] + "..."

        return text

    @staticmethod
    def _extract_text(event: CanonicalEvent) -> str:
        """Extract the full text from *event* without truncation.

        When the event carries a reply relation with ``fallback_text``,
        the text is augmented with a ``[replying to: ...]`` prefix
        before further processing.
        """
        # -- Relation fallback rendering ------------------------------------
        if event.relations:
            rel = event.relations[0]
            if rel.relation_type == "reply" and rel.fallback_text:
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                return f"[replying to: {rel.fallback_text}] {payload_text}"

        return str(event.payload.get("text", event.payload.get("body", "")))

    @staticmethod
    def _meshtastic_reply_id_from_relation(
        relation: object, target_adapter: str
    ) -> int | None:
        """Extract a numeric Meshtastic reply ID from a relation.

        Precedence:

        1. ``target_native_ref`` owned by *target_adapter* with a
           numeric ``native_message_id``.
        2. Relation ``metadata["meshtastic_reply_id"]`` (set by
           MatrixCodec for MMRelay-style emote reactions).
        3. ``None`` — no usable Meshtastic reply ID.

        Never uses a native ref from a different adapter.
        """
        ref = getattr(relation, "target_native_ref", None)
        if ref is not None:
            adapter = getattr(ref, "adapter", None)
            if adapter == target_adapter:
                mid = getattr(ref, "native_message_id", None)
                if mid is not None:
                    try:
                        return int(mid)
                    except (ValueError, TypeError):
                        pass

        # Fallback: MMRelay meshtastic_reply_id from relation metadata
        metadata = getattr(relation, "metadata", None) or {}
        mmrelay_id = metadata.get("meshtastic_reply_id")
        if mmrelay_id is not None:
            try:
                return int(str(mmrelay_id))
            except (ValueError, TypeError):
                pass

        return None

    @staticmethod
    def _resolve_emoji_text(rel: EventRelation, event: CanonicalEvent) -> str | None:
        """Resolve the reaction emoji/text using the standard priority order.

        Resolution order: ``rel.key`` → ``payload["key"]`` →
        ``payload["emoji"]`` → ``payload["body"]``.
        Each value is stripped; only non-empty results are returned.
        Returns ``None`` when no source yields a non-empty string.
        """
        if rel.key is not None:
            stripped = rel.key.strip()
            if stripped:
                return stripped
        for field in ("key", "emoji", "body"):
            _val = event.payload.get(field)
            if _val:
                stripped = str(_val).strip()
                if stripped:
                    return stripped
        return None

    @staticmethod
    def _plain_text(event: CanonicalEvent) -> str:
        """Extract plain text without relation fallback formatting."""
        return str(event.payload.get("text", event.payload.get("body", "")))
