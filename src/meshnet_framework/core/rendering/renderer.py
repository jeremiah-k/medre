"""Renderer protocol and pipeline for target-specific event rendering.

This module separates the *rendering* concern (converting a canonical event
into an adapter-ready payload) from both transforms and adapters.  A
:class:`Renderer` is a structural-typed protocol; any object satisfying the
``name``, ``can_render`` and ``render`` interface can be registered with the
:class:`RenderingPipeline`.

The pipeline tries registered renderers in priority order (lower value first)
until one accepts the event.  If no renderer matches a
:class:`ValueError` is raised.

Public symbols
--------------
* :class:`RenderingResult` – output of a rendering pass.
* :class:`Renderer` – protocol every renderer must satisfy.
* :class:`RenderingPipeline` – ordered dispatcher across renderers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from meshnet_framework.core.events import CanonicalEvent


# ---------------------------------------------------------------------------
# Rendering result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderingResult:
    """Output of a rendering pass.  Ready for adapter delivery.

    Attributes
    ----------
    event_id:
        The original canonical event ID.
    target_adapter:
        Which adapter this is rendered for.
    target_channel:
        Target channel if applicable.
    payload:
        Rendered payload in adapter-ready format.
    metadata:
        Rendering metadata (format hints, truncation info, etc.).
    truncated:
        Whether the rendered content was truncated.
    fallback_applied:
        Which fallback strategy was applied, if any.
    """

    event_id: str
    target_adapter: str
    target_channel: str | None
    payload: dict[str, object]
    metadata: dict[str, object] = field(default_factory=dict)
    truncated: bool = False
    fallback_applied: str | None = None


# ---------------------------------------------------------------------------
# Renderer protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Renderer(Protocol):
    """Protocol for target-specific renderers.

    A renderer converts a :class:`CanonicalEvent` into a
    :class:`RenderingResult` suitable for a particular adapter.  Renderers
    **must not** mutate the original event.
    """

    @property
    def name(self) -> str:
        """Renderer identifier."""
        ...

    def can_render(self, event: CanonicalEvent, target_adapter: str) -> bool:
        """Return ``True`` if this renderer can handle *event* for *target_adapter*."""
        ...

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Render *event* for delivery.  Must not mutate the original event."""
        ...


# ---------------------------------------------------------------------------
# Rendering pipeline
# ---------------------------------------------------------------------------

# Internal storage tuple: (priority, registration_order, renderer)
_PrioritisedRenderer = tuple[int, int, Renderer]


class RenderingPipeline:
    """Manages ordered renderers and dispatches events to the first match.

    Renderers are registered with an integer *priority* (lower values are
    checked first).  When :meth:`render` is called the pipeline walks
    renderers in priority order until one returns ``True`` from
    :meth:`Renderer.can_render`, then delegates to that renderer's
    :meth:`Renderer.render`.

    Raises
    ------
    ValueError
        If no registered renderer can handle the given event / adapter pair.
    """

    def __init__(self) -> None:
        self._renderers: list[_PrioritisedRenderer] = []
        self._seq: int = 0

    def register(self, renderer: Renderer, priority: int = 100) -> None:
        """Register a renderer.

        Parameters
        ----------
        renderer:
            Any object satisfying the :class:`Renderer` protocol.
        priority:
            Lower values are checked first.  Defaults to ``100``.
        """
        self._renderers.append((priority, self._seq, renderer))
        self._seq += 1
        # Stable sort: priority first, registration order breaks ties.
        self._renderers.sort(key=lambda t: (t[0], t[1]))

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
    ) -> RenderingResult:
        """Try renderers in priority order until one can render.

        Parameters
        ----------
        event:
            The canonical event to render.
        target_adapter:
            Name of the target adapter.
        target_channel:
            Optional target channel / conversation.

        Returns
        -------
        RenderingResult
            The rendered output from the first matching renderer.

        Raises
        ------
        ValueError
            If no registered renderer can handle the event.
        """
        for _pri, _seq, renderer in self._renderers:
            if renderer.can_render(event, target_adapter):
                return await renderer.render(event, target_adapter, target_channel)

        raise ValueError(
            f"No renderer registered for event_kind={event.event_kind!r} "
            f"target_adapter={target_adapter!r}"
        )
