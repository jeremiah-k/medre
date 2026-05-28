"""Plain-text renderer for radio transports and fallback channels.

The :class:`TextRenderer` handles event kinds that carry a natural plain-text
representation — message lifecycle events and presence changes — and converts
them into a simple ``{"text": ...}`` payload suitable for text-only targets
such as Meshtastic radio transports, SMS gateways, or terminal adapters.

Text is capped at a default of **500 characters**.  The cap is overridden
by ``RenderingContext.max_text_chars`` from adapter capabilities.  Negative or
zero caps are clamped to zero, producing empty output.  When truncation
occurs the resulting :class:`~medre.core.rendering.renderer.RenderingResult`
has its ``truncated`` flag set to ``True`` and the ``metadata`` dict includes
the original content length.

**Strategy fallback**: when ``RenderingContext.delivery_strategy`` is
``"fallback_text"``, the renderer sets ``fallback_applied="strategy_fallback_text"``
on the result, indicating degraded rendering was used because the target
adapter lacks native support for the event's relation type (reactions,
edits, deletes, replies).
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent, EventKind, EventRelation
from medre.core.rendering.renderer import RenderingContext, RenderingResult

# Maximum characters for rendered text before truncation.
_MAX_TEXT_LENGTH: int = 500


class TextRenderer:
    """Renderer for text-only targets (radio transports, fallback channels).

    Handles ``message.text``, ``message.created``, ``message.edited``,
    ``message.deleted``, ``message.reacted``, ``presence.changed``, and
    ``plugin.custom`` events.

    **Relation degradation** — When a canonical event carries relations
    (reply, reaction, edit, delete, thread) only the **first** relation
    is processed.  Each relation type produces deterministic degraded
    text that never emits empty or ambiguous output, even when relation
    metadata is partially missing.  See :meth:`_extract_text` for the
    exact fallback format per relation type.
    """

    name: str = "text"

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` for event kinds that have a plain-text representation.

        This renderer is platform-agnostic — it handles events based on
        event kind, not target adapter identity.  The rendering context
        is accepted for protocol compliance but is not used for
        discrimination.

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
            Whether this renderer can produce text output for the event.
        """
        return event.event_kind in (
            EventKind.MESSAGE_TEXT,
            EventKind.MESSAGE_CREATED,
            EventKind.MESSAGE_EDITED,
            EventKind.MESSAGE_DELETED,
            EventKind.MESSAGE_REACTED,
            EventKind.PRESENCE_CHANGED,
            EventKind.PLUGIN_CUSTOM,
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render a canonical event as plain text.

        Rendering rules by event kind:

        * ``message.text`` / ``message.created`` — extract ``payload["text"]``.
        * ``message.edited`` — prepend ``"[edited] "`` to the text.
        * ``message.deleted`` — return ``"[deleted]"``.
        * ``presence.changed`` — format as ``"{user} is now {status}"``.
        * ``plugin.custom`` — extract ``payload["text"]`` if available.

        **Relation fallback rendering** — when the event carries relations,
        only the **first** relation is processed:

        * *reply* — ``"[replying to: {target}] {payload_text}"`` with
          optional ``"by {sender}"`` enrichment when relation metadata
          carries ``sender_displayname`` / ``displayname`` / ``sender``.
          Target is resolved from ``fallback_text``, ``target_event_id``
          (abbreviated), or ``target_native_ref``.
        * *reaction* — ``"{actor} reacted with {key}"``.  Key is resolved
          from ``rel.key``, ``payload["emoji"]``, or ``payload["body"]``.
          When no key exists, produces ``"{actor} reacted"``.
        * *edit* — ``"[edited] {payload_text}"``, or ``"[edited]"`` when
          the edit carries no body.
        * *delete* — ``"[deleted: {target}]"`` when target context is
          available, or ``"[deleted]"`` when it is not.
        * *thread* — ``"[thread: {target}] {payload_text}"``, degraded
          similarly to reply.

        Text exceeding the character limit is truncated and the ``truncated``
        flag is set on the result.

        The original event is **never** mutated.

        Parameters
        ----------
        event:
            The canonical event to render.
        ctx:
            Frozen rendering context carrying the target adapter, delivery
            strategy, and text budget.

        Returns
        -------
        RenderingResult
            The rendered text payload wrapped in a standard result.
        """
        raw_text = self._extract_text(event)
        text, truncated = self._truncate(raw_text, max_text_chars=ctx.max_text_chars)

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(raw_text),
        }

        fallback_applied: str | None = None
        if event.relations:
            rel = event.relations[0]
            if rel.relation_type in (
                "reply",
                "reaction",
                "edit",
                "delete",
                "thread",
            ):
                fallback_applied = f"relation_{rel.relation_type}"

        # Strategy fallback takes precedence over relation-based fallback.
        # When strategy_fallback_text overrides a relation-based fallback,
        # preserve the underlying relation type in metadata so that
        # diagnostics and consumers can identify both the strategy and the
        # degraded relation in a single result.
        if ctx.delivery_strategy == "fallback_text":
            if fallback_applied is not None:
                metadata["strategy_relation_type"] = event.relations[0].relation_type
            fallback_applied = "strategy_fallback_text"

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload={"text": text},
            metadata=metadata,
            truncated=truncated,
            fallback_applied=fallback_applied,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_target_display(rel: EventRelation) -> str:
        """Resolve a human-readable display string for the relation target.

        Resolution order:

        1. ``rel.fallback_text`` — pre-computed fallback from the upstream
           codec.
        2. ``rel.target_event_id`` — abbreviated to first 8 characters for
           readability (with ``…`` when truncated).
        3. ``rel.target_native_ref.native_message_id`` — native-space ID
           when the canonical ID has not been resolved.
        4. Literal ``"unknown message"`` — explicit degraded text when no
           target reference is available at all.
        """
        if rel.fallback_text:
            return rel.fallback_text
        if rel.target_event_id:
            eid = rel.target_event_id
            return f"{eid[:8]}…" if len(eid) > 8 else eid
        if (
            rel.target_native_ref is not None
            and rel.target_native_ref.native_message_id
        ):
            return rel.target_native_ref.native_message_id
        return "unknown message"

    @staticmethod
    def _resolve_actor(event: CanonicalEvent) -> str:
        """Resolve the best available actor display name.

        Tries ``payload["displayname"]``, ``payload["user"]``, then
        falls back to ``event.source_adapter``.
        """
        return str(
            event.payload.get("displayname")
            or event.payload.get("user")
            or event.source_adapter
        )

    @staticmethod
    def _resolve_reaction_key(rel: EventRelation, event: CanonicalEvent) -> str | None:
        """Resolve the reaction key (emoji or label).

        Resolution order:

        1. ``rel.key`` — canonical reaction key set by the codec.
        2. ``payload["key"]`` — reaction key from the event payload.
        3. ``payload["emoji"]`` — common convention for emoji payload.
        4. ``payload["body"]`` — last-resort text body.

        Returns ``None`` only when no key-like value exists in any of
        these locations.
        """
        if rel.key:
            return rel.key
        key = event.payload.get("key")
        if key:
            return str(key)
        emoji = event.payload.get("emoji")
        if emoji:
            return str(emoji)
        body = event.payload.get("body")
        if body:
            return str(body)
        return None

    @staticmethod
    def _extract_text(event: CanonicalEvent) -> str:
        """Extract the raw (pre-truncation) text from *event*.

        When the event carries relations the text is augmented with
        fallback formatting before kind-based logic is applied.

        Both ``payload["text"]`` and ``payload["body"]`` are checked —
        adapters use either key depending on their native format (Matrix
        and Meshtastic codecs store text under ``"body"``, others may
        use ``"text"``).

        **Relation handling** — When ``event.relations`` is non-empty
        only the **first** relation is processed.  This is a deliberate
        design choice: canonical events may carry multiple relations but
        the generic text renderer produces degraded output for a single
        relation to keep the text readable on constrained displays.
        Events carrying more than one relation should be handled by
        platform-specific renderers where possible.
        """
        kind = event.event_kind

        # -- Relation fallback rendering ------------------------------------
        #
        # Only the first relation is processed.  See docstring above for
        # rationale.  Each branch produces deterministic, meaningful
        # degraded text even when relation metadata is partially or
        # fully missing — the renderer never emits empty or ambiguous
        # fallback when relation payload data exists.
        if event.relations:
            rel = event.relations[0]

            if rel.relation_type == "reply":
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                target = TextRenderer._resolve_target_display(rel)
                # Enrich with sender/displayname from relation metadata
                # when the upstream codec populated it.
                sender_display = (
                    rel.metadata.get("sender_displayname")
                    or rel.metadata.get("displayname")
                    or rel.metadata.get("sender")
                )
                prefix = f"[replying to: {target}"
                if sender_display:
                    prefix += f" by {sender_display}"
                prefix += "]"
                if payload_text:
                    return f"{prefix} {payload_text}"
                return prefix

            if rel.relation_type == "reaction":
                actor = TextRenderer._resolve_actor(event)
                key = TextRenderer._resolve_reaction_key(rel, event)
                if key:
                    return f"{actor} reacted with {key}"
                # Degraded: payload has reaction relation but no
                # identifiable key/emoji/body.  Produce meaningful text
                # rather than falling through to kind-based rendering.
                return f"{actor} reacted"

            if rel.relation_type == "edit":
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                if payload_text:
                    return f"[edited] {payload_text}"
                # Deterministic degraded output when edit carries no body.
                return "[edited]"

            if rel.relation_type == "delete":
                target = TextRenderer._resolve_target_display(rel)
                # Include target/original context when available.
                if target != "unknown message":
                    return f"[deleted: {target}]"
                return "[deleted]"

            if rel.relation_type == "thread":
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                target = TextRenderer._resolve_target_display(rel)
                if payload_text:
                    return f"[thread: {target}] {payload_text}"
                return f"[thread: {target}]"

        # -- Kind-based rendering -------------------------------------------
        if kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            return str(event.payload.get("text", event.payload.get("body", "")))

        if kind == EventKind.MESSAGE_EDITED:
            return "[edited] " + str(
                event.payload.get("text", event.payload.get("body", ""))
            )

        if kind == EventKind.MESSAGE_DELETED:
            return "[deleted]"

        if kind == EventKind.MESSAGE_REACTED:
            # Without a relation, render the payload text if present.
            return str(event.payload.get("text", event.payload.get("body", "")))

        if kind == EventKind.PRESENCE_CHANGED:
            user = str(event.payload.get("user", "unknown"))
            status = str(event.payload.get("status", "unknown"))
            return f"{user} is now {status}"

        if kind == EventKind.PLUGIN_CUSTOM:
            return str(event.payload.get("text", event.payload.get("body", "")))

        # Defensive fallback for unrecognised kinds that slip through
        # can_render (should not happen in practice).
        return str(event.payload.get("text", event.payload.get("body", "")))

    @staticmethod
    def _truncate(
        text: str,
        *,
        max_text_chars: int | None = None,
    ) -> tuple[str, bool]:
        """Cap *text* at the configured character limit.

        Parameters
        ----------
        text:
            The text to potentially truncate.
        max_text_chars:
            Maximum characters to allow.  When ``None``, falls back to
            the module-level default :data:`_MAX_TEXT_LENGTH` (500).

        Returns
        -------
        tuple[str, bool]
            The (possibly truncated) text and whether truncation occurred.
        """
        limit = max(
            0, max_text_chars if max_text_chars is not None else _MAX_TEXT_LENGTH
        )
        if limit == 0 and text:
            return "", True
        if len(text) <= limit:
            return text, False
        return text[:limit], True
