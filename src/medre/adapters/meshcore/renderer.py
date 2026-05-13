"""MeshCore renderer for target-specific event rendering.

The :class:`MeshCoreRenderer` converts canonical events into
MeshCore-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

This renderer is owned by the MeshCore adapter package and is registered
with the rendering pipeline.

Selection is via the rendering pipeline's platform registry: when the
pipeline populates the adapter's platform as ``"meshcore"``, the renderer
matches on that platform string directly.  This decouples renderer
selection from adapter naming conventions.

**Tranche 1 scope**: text messages only.  Length-limit enforcement is
noted but not applied; full enforcement is deferred to a later tranche.
"""
from __future__ import annotations

from medre.core.events import CanonicalEvent
from medre.core.rendering.renderer import RenderingResult


class MeshCoreRenderer:
    """Renderer for MeshCore transport targets.

    Produces content dicts with ``text``, ``channel_index``, and optional
    ``meshnet_name``.

    Selection is via the pipeline's platform registry.  See module
    docstring for details.
    """

    name: str = "meshcore"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshcore"
    """Internal platform identifier for matching via ``target_platform``."""

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` when *target_platform* is ``"meshcore"``.

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.
        target_platform:
            Platform name of the target adapter, supplied by the
            rendering pipeline's platform registry.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        return target_platform == self._PLATFORM

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render a canonical event into a MeshCore content payload.

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
            The rendered MeshCore content dict wrapped in a result.
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

        # TODO(tranche-N): enforce truncation for MeshCore payload limits.
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
