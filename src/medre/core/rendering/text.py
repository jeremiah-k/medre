"""Plain-text renderer for radio transports and fallback channels.

The :class:`TextRenderer` handles event kinds that carry a natural plain-text
representation — message lifecycle events and presence changes — and converts
them into a simple ``{"text": ...}`` payload suitable for text-only targets
such as Meshtastic radio transports, SMS gateways, or terminal adapters.

Text is capped at a default of **500 characters**.  The cap can be overridden
per-call via ``max_text_chars`` (from adapter capabilities).  Negative or
zero caps are clamped to zero, producing empty output.  When truncation
occurs the resulting :class:`~medre.core.rendering.renderer.RenderingResult`
has its ``truncated`` flag set to ``True`` and the ``metadata`` dict includes
the original content length.

**Strategy fallback**: when ``delivery_strategy="fallback_text"`` is passed,
the renderer sets ``fallback_applied="strategy_fallback_text"`` on the result,
indicating degraded rendering was used because the target adapter lacks
native support for the event's relation type (reactions, edits, deletes,
replies).
"""

from __future__ import annotations

from medre.core.events import CanonicalEvent, EventKind
from medre.core.rendering.renderer import RenderingResult

# Maximum characters for rendered text before truncation.
_MAX_TEXT_LENGTH: int = 500


class TextRenderer:
    """Renderer for text-only targets (radio transports, fallback channels).

    Handles ``message.text``, ``message.created``, ``message.edited``,
    ``message.deleted``, ``message.reacted``, ``presence.changed``, and
    ``plugin.custom`` events.
    """

    name: str = "text"

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` for event kinds that have a plain-text representation.

        This renderer is platform-agnostic — it handles events based on
        event kind, not target adapter identity.  The *target_adapter* and
        *target_platform* parameters are accepted for protocol compatibility
        but are not used for discrimination.

        Parameters
        ----------
        event:
            The canonical event to check.
        target_adapter:
            Name of the target adapter (not used for discrimination).
        target_platform:
            Platform name (not used for discrimination).

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
        target_adapter: str,
        target_channel: str | None = None,
        *,
        max_text_chars: int | None = None,
        delivery_strategy: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event as plain text.

        Rendering rules by event kind:

        * ``message.text`` / ``message.created`` — extract ``payload["text"]``.
        * ``message.edited`` — prepend ``"[edited] "`` to the text.
        * ``message.deleted`` — return ``"[deleted]"``.
        * ``presence.changed`` — format as ``"{user} is now {status}"``.
        * ``plugin.custom`` — extract ``payload["text"]`` if available.

        **Relation fallback rendering** — when the event carries relations:

        * *reply* — ``"[replying to: {fallback_text}] {payload_text}"``
        * *reaction* — ``"{actor} reacted with {key}"``
        * *edit* — ``"[edited] {payload_text}"``

        Text exceeding the character limit is truncated and the ``truncated``
        flag is set on the result.

        The original event is **never** mutated.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the rendered payload is intended for.
        target_channel:
            Optional channel / conversation on the target adapter.
        max_text_chars:
            Per-call character cap for the rendered text.  When ``None``
            (the default), the 500-character module-level default is used.
            When provided, the text is truncated to this many characters
            and the ``truncated`` flag is set on the result when
            truncation occurs.

        Returns
        -------
        RenderingResult
            The rendered text payload wrapped in a standard result.
        """
        raw_text = self._extract_text(event)
        text, truncated = self._truncate(raw_text, max_text_chars=max_text_chars)

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(raw_text),
        }

        fallback_applied: str | None = None
        if event.relations:
            rel = event.relations[0]
            if rel.relation_type in ("reply", "reaction", "edit"):
                fallback_applied = f"relation_{rel.relation_type}"

        # Strategy fallback takes precedence over relation-based fallback.
        if delivery_strategy == "fallback_text":
            fallback_applied = "strategy_fallback_text"

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
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

        When the event carries relations the text is augmented with
        fallback formatting before kind-based logic is applied.

        Both ``payload["text"]`` and ``payload["body"]`` are checked —
        adapters use either key depending on their native format (Matrix
        and Meshtastic codecs store text under ``"body"``, others may
        use ``"text"``).
        """
        kind = event.event_kind

        # -- Relation fallback rendering ------------------------------------
        if event.relations:
            rel = event.relations[0]
            if rel.relation_type == "reply" and rel.fallback_text:
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                return f"[replying to: {rel.fallback_text}] {payload_text}"

            if rel.relation_type == "reaction" and rel.key:
                actor = str(event.payload.get("user", event.source_adapter))
                return f"{actor} reacted with {rel.key}"

            if rel.relation_type == "edit":
                payload_text = str(
                    event.payload.get("text", event.payload.get("body", ""))
                )
                return f"[edited] {payload_text}"

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
