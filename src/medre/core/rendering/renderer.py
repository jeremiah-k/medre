"""Renderer protocol and pipeline for target-specific event rendering.

**Rendering Boundary Separation**

Renderer produces :class:`RenderingResult`.  Adapters consume
:class:`RenderingResult`.  No adapter shall perform rendering logic.  No
renderer shall deliver.

This module enforces the rendering boundary: the *rendering* concern
(converting a canonical event into an adapter-ready payload) is strictly
separated from both transforms and adapters.  A :class:`Renderer` is a
structural-typed protocol; any object satisfying the ``name``,
``can_render`` and ``render`` interface can be registered with the
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

from medre.core.events import CanonicalEvent


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

    Renderer produces :class:`RenderingResult`.  Adapters consume
    :class:`RenderingResult`.  No adapter shall perform rendering logic.
    No renderer shall deliver.

    A renderer converts a :class:`CanonicalEvent` into a
    :class:`RenderingResult` suitable for a particular adapter.  Renderers
    **must not** mutate the original event.

    **Platform-aware dispatch**

    When ``target_platform`` is provided (via the rendering pipeline's
    platform registry), renderers match on it directly.  The platform
    string is the authoritative identifier — adapter IDs are routing
    identifiers and should not be overloaded with platform semantics.
    """

    @property
    def name(self) -> str:
        """Renderer identifier."""
        ...

    def can_render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_platform: str | None = None,
    ) -> bool:
        """Return ``True`` if this renderer can handle *event* for *target_adapter*.

        Parameters
        ----------
        event:
            The canonical event to check.
        target_adapter:
            Name of the target adapter (routing identifier).
        target_platform:
            Platform name of the target adapter, or ``None`` if unknown.
            Renderers should match on this directly.
        """
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

    **Platform registry**

    The pipeline maintains an optional ``adapter_platforms`` mapping from
        adapter ID to platform name (e.g. ``"local-radio"`` → ``"radio-alpha"``).
    When populated, the pipeline passes the platform to each renderer's
    ``can_render()``, enabling platform-aware dispatch.

    Populate the registry via :meth:`register_adapter_platform` or
    :meth:`register_platforms_from`.  The pipeline runner automatically
    populates it from adapter metadata on startup.

    Raises
    ------
    ValueError
        If no registered renderer can handle the given event / adapter pair.
    """

    def __init__(self) -> None:
        self._renderers: list[_PrioritisedRenderer] = []
        self._seq: int = 0
        self._adapter_platforms: dict[str, str] = {}

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

    # -- Platform registry --------------------------------------------------

    def register_adapter_platform(self, adapter_id: str, platform: str) -> None:
        """Register a single adapter's platform.

        Once registered, the pipeline passes the platform string to each
        renderer's ``can_render()`` so that renderers can match on
        platform identity rather than adapter-name heuristics.

        Parameters
        ----------
        adapter_id:
            The adapter instance ID (e.g. ``"local-radio"``).
        platform:
            The platform name (e.g. ``"radio-alpha"``, ``"radio-bravo"``).
        """
        self._adapter_platforms[adapter_id] = platform

    def register_platforms_from(self, platforms: dict[str, str]) -> None:
        """Register multiple adapter platforms at once.

        Parameters
        ----------
        platforms:
            Mapping of adapter ID to platform name.
        """
        self._adapter_platforms.update(platforms)

    def get_platform(self, adapter_id: str) -> str | None:
        """Return the platform name for *adapter_id*, or ``None`` if
        not registered."""
        return self._adapter_platforms.get(adapter_id)

    def status_summary(self) -> dict[str, object]:
        """Return a read-only snapshot of pipeline state for diagnostics.

        Returns a plain dict safe for JSON serialisation.  Does **not**
        expose renderer references.

        Returns
        -------
        dict[str, object]
            Keys: ``renderer_count``, ``renderer_names``, ``platform_registry``.
        """
        return {
            "renderer_count": len(self._renderers),
            "renderer_names": sorted(r.name for _, _, r in self._renderers),
            "platform_registry": dict(sorted(self._adapter_platforms.items())),
        }

    # -- Rendering ----------------------------------------------------------

    async def render(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
        *,
        target_platform: str | None = None,
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
        target_platform:
            Optional platform name of the target adapter.  When not
            provided the pipeline looks up the adapter's platform from
            its internal registry; if still unknown, ``None`` is passed
            to renderers.

        Returns
        -------
        RenderingResult
            The rendered output from the first matching renderer.

        Raises
        ------
        ValueError
            If no registered renderer can handle the event.
        """
        # Resolve platform from explicit param or internal registry.
        platform = target_platform if target_platform is not None else self._adapter_platforms.get(target_adapter)

        for _pri, _seq, renderer in self._renderers:
            if renderer.can_render(event, target_adapter, platform):
                return await renderer.render(event, target_adapter, target_channel)

        raise ValueError(
            f"No renderer registered for event_kind={event.event_kind!r} "
            f"target_adapter={target_adapter!r}"
        )
