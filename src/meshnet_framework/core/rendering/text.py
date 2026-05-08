"""Plain-text renderer for radio transports and fallback channels.

The :class:`TextRenderer` handles event kinds that carry a natural plain-text
representation — message lifecycle events and presence changes — and converts
them into a simple ``{"text": ...}`` payload suitable for text-only targets
such as Meshtastic radio transports, SMS gateways, or terminal adapters.

Text is capped at **500 characters**.  When truncation occurs the resulting
:class:`~meshnet_framework.core.rendering.renderer.RenderingResult` has its
``truncated`` flag set to ``True`` and the ``metadata`` dict includes the
original content length.
"""

from __future__ import annotations

from meshnet_framework.core.events import CanonicalEvent, EventKind
from meshnet_framework.core.rendering.renderer import RenderingResult

# Maximum characters for rendered text before truncation.
_MAX_TEXT_LENGTH: int = 500


class TextRenderer:
    """Renderer for text-only targets (radio transports, fallback channels).

    Handles ``message.text``, ``message.created``, ``message.edited``,
    ``message.deleted``, ``presence.changed``, and ``plugin.custom`` events.
    """

    name: str = "text"

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(self, event: CanonicalEvent, target_adapter: str) -> bool:
        """Return ``True`` for event kinds that have a plain-text representation.

        Parameters
        ----------
        event:
            The canonical event to check.
        target_adapter:
            Name of the target adapter (not used for capability
            discrimination in this renderer).

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
    ) -> RenderingResult:
        """Render a canonical event as plain text.

        Rendering rules by event kind:

        * ``message.text`` / ``message.created`` — extract ``payload["text"]``.
        * ``message.edited`` — prepend ``"[edited] "`` to the text.
        * ``message.deleted`` — return ``"[deleted]"``.
        * ``presence.changed`` — format as ``"{user} is now {status}"``.
        * ``plugin.custom`` — extract ``payload["text"]`` if available.

        Text exceeding 500 characters is truncated with an ellipsis marker
        and the ``truncated`` flag is set on the result.

        The original event is **never** mutated.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the rendered payload is intended for.
        target_channel:
            Optional channel / conversation on the target adapter.

        Returns
        -------
        RenderingResult
            The rendered text payload wrapped in a standard result.
        """
        raw_text = self._extract_text(event)
        text, truncated = self._truncate(raw_text)

        metadata: dict[str, object] = {
            "renderer": self.name,
            "original_length": len(raw_text),
        }

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload={"text": text},
            metadata=metadata,
            truncated=truncated,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(event: CanonicalEvent) -> str:
        """Extract the raw (pre-truncation) text from *event*."""
        kind = event.event_kind

        if kind in (EventKind.MESSAGE_TEXT, EventKind.MESSAGE_CREATED):
            return str(event.payload.get("text", ""))

        if kind == EventKind.MESSAGE_EDITED:
            return "[edited] " + str(event.payload.get("text", ""))

        if kind == EventKind.MESSAGE_DELETED:
            return "[deleted]"

        if kind == EventKind.PRESENCE_CHANGED:
            user = str(event.payload.get("user", "unknown"))
            status = str(event.payload.get("status", "unknown"))
            return f"{user} is now {status}"

        if kind == EventKind.PLUGIN_CUSTOM:
            return str(event.payload.get("text", ""))

        # Defensive fallback for unrecognised kinds that slip through
        # can_render (should not happen in practice).
        return str(event.payload.get("text", ""))

    @staticmethod
    def _truncate(text: str) -> tuple[str, bool]:
        """Cap *text* at :data:`_MAX_TEXT_LENGTH` characters.

        Returns
        -------
        tuple[str, bool]
            The (possibly truncated) text and whether truncation occurred.
        """
        if len(text) <= _MAX_TEXT_LENGTH:
            return text, False
        return text[:_MAX_TEXT_LENGTH], True
