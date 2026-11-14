"""Plain-text renderer for radio transports and fallback channels.

The :class:`TextRenderer` handles event kinds that carry a natural plain-text
representation — message lifecycle events and presence changes — and converts
them into a simple ``{"text": ...}`` payload suitable for text-only targets
such as Meshtastic radio transports, SMS gateways, or terminal adapters.

Text is capped at a default of **500 characters** (defined in
``text_helpers._DEFAULT_MAX_TEXT_LENGTH``).  The cap is overridden
by ``RenderingContext.max_text_chars`` from adapter capabilities and further
bounded by ``RenderingContext.max_text_bytes`` when provided. Negative or
zero caps are clamped to zero, producing empty output. When truncation
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

from medre.core.events import CanonicalEvent, EventKind
from medre.core.rendering.renderer import (
    FallbackApplied,
    RenderingContext,
    RenderingResult,
)
from medre.core.rendering.text_helpers import (
    extract_event_text,
)
from medre.core.rendering.text_helpers import truncate_text as _shared_truncate_text
from medre.core.rendering.text_helpers import truncate_text_bytes

#: Mapping from relation type to the canonical :data:`FallbackApplied` value.
_RELATION_FALLBACK_MAP: dict[str, FallbackApplied] = {
    "reply": "relation_reply",
    "reaction": "relation_reaction",
    "edit": "relation_edit",
    "delete": "relation_delete",
    "thread": "relation_thread",
}


class TextRenderer:
    """Renderer for text-only targets (radio transports, fallback channels).

    Handles ``message.text``, ``message.created``, ``message.edited``,
    ``message.deleted``, ``message.reacted``, ``presence.changed``, and
    ``plugin.custom`` events.

    **Relation degradation** — When a canonical event carries relations
    (reply, reaction, edit, delete, thread), every unrepresented relation
    contributes deterministic readable context before the authored body.
    See :func:`medre.core.rendering.relations.compose_relation_text`.
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

        **Relation fallback rendering** describes every unrepresented
        relation through the shared inline policy. Authored message text
        appears once; reaction events require an exact explicit key and
        use only supplied target context for their preview.

        Text exceeding the character or UTF-8 byte limit is truncated on a
        character boundary and the ``truncated`` flag is set on the result.

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
        text, truncated_chars = self._truncate(
            raw_text, max_text_chars=ctx.max_text_chars
        )
        text, truncated_bytes, _, _ = truncate_text_bytes(text, ctx.max_text_bytes)
        truncated = truncated_chars or truncated_bytes

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(raw_text),
        }

        fallback_applied: FallbackApplied | None = None
        if event.relations:
            rel = event.relations[0]
            fallback_applied = _RELATION_FALLBACK_MAP.get(rel.relation_type)

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
    def _extract_text(event: CanonicalEvent) -> str:
        """Extract the raw (pre-truncation) text from *event*.

        Delegates to :func:`~medre.core.rendering.text_helpers.extract_event_text`.
        """
        return extract_event_text(event)

    @staticmethod
    def _truncate(
        text: str,
        *,
        max_text_chars: int | None = None,
    ) -> tuple[str, bool]:
        """Cap *text* at the configured character limit.

        Delegates to :func:`~medre.core.rendering.text_helpers.truncate_text`.
        """
        return _shared_truncate_text(text, max_text_chars=max_text_chars)
