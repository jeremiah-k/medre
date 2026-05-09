"""Meshtastic renderer for target-specific event rendering.

The :class:`MeshtasticRenderer` converts canonical events into
Meshtastic-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

This renderer is owned by the Meshtastic adapter package and is registered
with the rendering pipeline.

The renderer supports three selection strategies, checked in order:

1. **Platform match** — when the rendering pipeline's platform registry
   is populated, it passes the target adapter's platform (``"meshtastic"``)
   to ``can_render``.  This is the primary selection path.

2. **Adapter-name prefix** — ``target_adapter.startswith("meshtastic")``
   serves as a simple convention-based fallback when the platform registry
   is not populated.

3. **Explicit adapter IDs** — the ``known_adapters`` constructor set allows
   realistic adapter IDs like ``"local-radio"`` to be matched without
   requiring a ``"meshtastic"`` name prefix.

**Tranche 1 scope**: text messages only.  Length-limit enforcement is
noted but not applied; full enforcement is deferred to a later tranche.
"""
from __future__ import annotations

from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult


class MeshtasticRenderer:
    """Renderer for Meshtastic transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    Parameters
    ----------
    known_adapters:
        Optional set of adapter IDs that this renderer should handle.
        Useful for realistic IDs like ``"local-radio"`` that do not
        start with the ``"meshtastic"`` prefix.
    """

    name: str = "meshtastic"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshtastic"
    """Internal platform identifier for matching via ``target_platform``."""

    def __init__(self, known_adapters: set[str] | None = None) -> None:
        self._known_adapters: set[str] = known_adapters or set()

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_adapter* is a Meshtastic target.

        Three selection strategies are checked in order (first match wins):

        1. **Platform match** — ``target_platform == "meshtastic"``.
        2. **Adapter-name prefix** — ``target_adapter`` starts with
           ``"meshtastic"``.
        3. **Explicit adapter IDs** — ``target_adapter`` is in the
           ``known_adapters`` set passed at construction.

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.
        target_platform:
            Platform name of the target adapter.  ``None`` when the
            pipeline registry is not populated.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        if target_platform == self._PLATFORM:
            return True
        if target_adapter.startswith("meshtastic"):
            return True
        return target_adapter in self._known_adapters

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into a Meshtastic content payload.

        The rendered payload includes:

        * ``text``: extracted text from the event payload.
        * ``channel_index``: parsed from *target_channel* or ``0``.
        * ``meshnet_name``: empty string placeholder.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the adapter the payload is intended for.
        target_channel:
            Target channel identifier; parsed as an integer channel index.

        Returns
        -------
        RenderingResult
            The rendered Meshtastic content dict wrapped in a result.
        """
        text = str(event.payload.get("body", event.payload.get("text", "")))

        # Parse channel index from target_channel
        channel_index = 0
        if target_channel is not None:
            try:
                channel_index = int(target_channel)
            except (ValueError, TypeError):
                channel_index = 0

        content: dict[str, object] = {
            "text": text,
            "channel_index": channel_index,
            "meshnet_name": "",
        }

        metadata: dict[str, object] = {
            "renderer": self.name,
        }

        # TODO(tranche-N): Meshtastic has a ~228 byte payload limit.
        # Future tranches should enforce truncation here.
        # For now we pass through.
        truncated = False

        return RenderingResult(
            event_id=event.event_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            payload=content,
            metadata=metadata,
            truncated=truncated,
        )
