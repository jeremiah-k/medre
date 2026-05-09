"""MeshCore renderer for target-specific event rendering.

The :class:`MeshCoreRenderer` converts canonical events into
MeshCore-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

This renderer is owned by the MeshCore adapter package and is registered
with the rendering pipeline.

Three selection strategies are available (checked in order, first match
wins):

**Platform match**

When the rendering pipeline's platform registry is populated, the pipeline
passes the target adapter's platform (``"meshcore"``) to ``can_render``.
The renderer matches on this platform string directly, independent of the
adapter's instance ID.  Realistic IDs like ``"local-radio"`` work without
naming conventions.

**Adapter-name prefix**

When ``target_platform`` is ``None``, the renderer checks whether
``target_adapter.startswith("meshcore")``.  This is useful for adapters
whose IDs follow the platform naming convention.

**Explicit adapter IDs (``known_adapters``)**

The ``known_adapters`` constructor accepts a set of adapter IDs that this
renderer should handle regardless of naming convention.  This supports
realistic IDs like ``"local-radio"`` that do not start with the
``"meshcore"`` prefix.

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

    Three selection strategies are supported: platform match, adapter-name
    prefix, and explicit adapter IDs.  See module docstring for details.

    Parameters
    ----------
    known_adapters:
        Optional set of adapter IDs that this renderer should handle.
        Useful for realistic IDs like ``"local-radio"`` that do not start
        with the ``"meshcore"`` prefix.
    """

    name: str = "meshcore"
    """Platform name this renderer handles (used by the rendering pipeline
    when platform registry is available)."""

    _PLATFORM: str = "meshcore"
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
        """Return ``True`` when *target_adapter* is a MeshCore target.

        Three selection strategies are checked in order (first match wins):

        1. **Platform match** — ``target_platform == "meshcore"``.
        2. **Adapter-name prefix** — ``target_adapter`` starts with
           ``"meshcore"``.
        3. **Known adapters** — ``target_adapter`` is in the explicit set
           passed at construction.

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
        # Strategy 1: platform match.
        if target_platform == self._PLATFORM:
            return True
        # Strategy 2: adapter-name prefix.
        if target_adapter.startswith("meshcore"):
            return True
        # Strategy 3: explicit known-adapters set.
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
