"""MeshCore renderer for target-specific event rendering.

The :class:`MeshCoreRenderer` converts canonical events into
MeshCore-ready content payloads (dicts with ``text``, ``channel_index``,
and optional ``meshnet_name``).

This renderer is owned by the MeshCore adapter package and is registered
with the rendering pipeline.  Renderer selection dispatches when the
``target_adapter`` starts with ``"meshcore"`` (convention) **or** is in
the set of *known_adapters* passed at construction (preferred for
realistic adapter IDs like ``"local-radio"`` or ``"garage-mesh"``).

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

    Renderer selection is **not** based solely on adapter ID prefix.
    A set of known MeshCore adapter IDs can be passed at construction
    time via *known_adapters*.  ``can_render`` matches if the target
    adapter starts with ``"meshcore"`` **or** is in the explicit set.
    This avoids forcing realistic adapter IDs like ``"local-radio"`` or
    ``"garage-mesh"`` to use an artificial prefix.

    .. todo::
        Long-term renderer selection should be driven by adapter registry
        or platform identity, not ad-hoc ``known_adapters`` sets passed
        to renderer constructors.  Adapter IDs should not need naming
        conventions.  The current explicit known-adapter registration is
        a tranche-1 mechanism.

    Parameters
    ----------
    known_adapters:
        Optional set of adapter IDs that this renderer should handle.
    """

    name: str = "meshcore"

    def __init__(self, known_adapters: set[str] | None = None) -> None:
        self._known_adapters: set[str] = known_adapters or set()

    # ------------------------------------------------------------------
    # Capability check
    # ------------------------------------------------------------------

    def can_render(self, event: CanonicalEvent, target_adapter: str) -> bool:
        """Return ``True`` when *target_adapter* is a MeshCore target.

        A target is considered MeshCore when:

        * its ID starts with ``"meshcore"`` (convention), **or**
        * its ID is in the *known_adapters* set passed at construction
          (preferred for realistic IDs).

        Parameters
        ----------
        event:
            The canonical event to check (not used for discrimination).
        target_adapter:
            Name of the target adapter.

        Returns
        -------
        bool
            Whether this renderer handles events for the given adapter.
        """
        return target_adapter.startswith("meshcore") or target_adapter in self._known_adapters

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
