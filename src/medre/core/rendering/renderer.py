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

**Strict rendering context**

Every renderer receives a frozen :class:`RenderingContext` carrying all
dispatch metadata — delivery strategy, target identity, capability
constraints, and text budgets.  The pipeline builds the context once per
render call and passes it to both ``can_render`` and ``render``.  Renderers
must not perform signature introspection; they implement one strict
signature.

Public symbols
--------------
* :class:`RenderingResult` – output of a rendering pass.
* :class:`RenderingContext` – frozen dispatch context for renderers.
* :class:`Renderer` – protocol every renderer must satisfy.
* :class:`RenderingPipeline` – ordered dispatcher across renderers.
* :data:`DeliveryStrategyMethod` – well-known delivery strategy values.
* :data:`CapabilityLevel` – renderer capability level values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal, Protocol, get_args, runtime_checkable

from medre.core.events import CanonicalEvent

# ---------------------------------------------------------------------------
# Strategy and capability types
# ---------------------------------------------------------------------------

#: Well-known delivery strategy method values, matching
#: :attr:`~medre.core.planning.delivery_plan.DeliveryStrategy.method`.
DeliveryStrategyMethod = Literal[
    "direct",
    "fallback_text",
    "skip",
    "propagated",
    "opportunistic",
    "paper",
]

#: Capability level for renderer discrimination.  Renderers preserve
#: these semantics — ``"native"`` for full platform-native rendering,
#: ``"fallback"`` for degraded but functional output, ``"unsupported"``
#: for targets that cannot handle the event at all.
CapabilityLevel = Literal["native", "fallback", "unsupported"]


# ---------------------------------------------------------------------------
# Rendering context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderingContext:
    """Frozen dispatch context passed to every renderer.

    The pipeline builds one :class:`RenderingContext` per render call and
    passes it to both :meth:`Renderer.can_render` and
    :meth:`Renderer.render`.  Renderers inspect the context to decide
    whether and how to render — they must not rely on external state or
    signature introspection.

    ``delivery_strategy`` is a *context hint*, not a renderer selector.
    When ``"fallback_text"``, renderers should produce degraded text
    output within their native format (e.g. a Matrix renderer still
    produces a Matrix msgtype/body, another renderer still produces
    its native payload fields).  The pipeline does **not** bypass
    target-native renderers based on this field.

    **Populated and reserved fields** — ``max_text_bytes`` is wired
    from adapter capabilities by the pipeline.  ``capability_level``
    and ``capability_policy`` are defined as part of the context
    protocol for forward compatibility and caller-provided plumbing,
    but the default pipeline does **not** populate them; they remain
    ``"native"`` and ``None`` respectively unless a caller explicitly
    sets them.  Renderers MUST treat ``delivery_strategy`` as the
    authoritative dispatch signal.

    Attributes
    ----------
    delivery_strategy:
        The resolved delivery strategy method.  Determines rendering
        mode: ``"direct"`` for normal native rendering,
        ``"fallback_text"`` for degraded text rendering, ``"skip"``
        for suppressed delivery.  This is the authoritative dispatch
        signal for renderers.
    target_adapter:
        Name of the target adapter (routing identifier).
    target_channel:
        Target channel / conversation, or ``None`` if not applicable.
    target_platform:
        Platform name of the target adapter, or ``None`` if unknown.
    max_text_chars:
        Maximum text length in characters from adapter capabilities,
        or ``None`` for no limit.
    max_text_bytes:
        Maximum text length in UTF-8 bytes from adapter capabilities,
        or ``None`` for no limit.  Wired from the target adapter's
        ``SIZE_LIMITS`` capability by the pipeline.
    capability_level:
        The target's capability level for the event's relation type:
        ``"native"`` (full support), ``"fallback"`` (degraded),
        ``"unsupported"`` (cannot handle).  Defaults to ``"native"``.
        **Reserved**: the default pipeline does not set this field
        from adapter capabilities; renderers should rely on
        ``delivery_strategy`` for dispatch unless a caller explicitly
        provides it.
    capability_policy:
        Optional policy hint governing rendering behaviour (e.g.
        ``"strict"`` for hard reject on capability mismatch,
        ``"lenient"`` for best-effort).  ``None`` when no policy is
        set.  **Reserved**: not currently wired by the default
        pipeline.
    """

    delivery_strategy: DeliveryStrategyMethod
    target_adapter: str
    target_channel: str | None = None
    target_platform: str | None = None
    max_text_chars: int | None = None
    max_text_bytes: int | None = None
    capability_level: CapabilityLevel = "native"
    capability_policy: str | None = None

    _VALID_STRATEGIES: ClassVar[frozenset[str]] = frozenset(
        get_args(DeliveryStrategyMethod)
    )

    def __post_init__(self) -> None:
        if self.delivery_strategy not in self._VALID_STRATEGIES:
            raise ValueError(
                f"Unknown delivery_strategy {self.delivery_strategy!r}. "
                f"Must be one of {sorted(self._VALID_STRATEGIES)}."
            )


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

    **Strict signature**

    Both ``can_render`` and ``render`` accept a frozen
    :class:`RenderingContext` carrying all dispatch metadata.  Renderers
    implement exactly one signature — no signature introspection, no
    compatibility shims.

    **Platform-aware dispatch**

    When ``target_platform`` is provided via the rendering context,
    renderers match on it directly.  The platform string is the
    authoritative identifier — adapter IDs are routing identifiers and
    should not be overloaded with platform semantics.

    **Delivery strategy as context hint**

    ``delivery_strategy`` in the context is a *hint* for the renderer,
    not a renderer selector.  When ``"fallback_text"``, the target-native
    renderer should still produce its native format but with degraded
    text content.  The pipeline does not bypass renderers based on
    strategy.
    """

    @property
    def name(self) -> str:
        """Renderer identifier."""
        ...

    def can_render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> bool:
        """Return ``True`` if this renderer can handle *event* for the target.

        Parameters
        ----------
        event:
            The canonical event to check.
        ctx:
            Frozen rendering context with target identity, delivery
            strategy, and capability metadata.
        """
        ...

    async def render(
        self,
        event: CanonicalEvent,
        ctx: RenderingContext,
    ) -> RenderingResult:
        """Render *event* for delivery.  Must not mutate the original event.

        Parameters
        ----------
        event:
            The canonical event to render.
        ctx:
            Frozen rendering context with target identity, delivery
            strategy, capability metadata, and text budgets.
        """
        ...


# ---------------------------------------------------------------------------
# Rendering pipeline
# ---------------------------------------------------------------------------

# Internal storage tuple: (priority, registration_order, renderer)
_PrioritisedRenderer = tuple[int, int, Renderer]


class RenderingPipeline:
    """Manages ordered renderers and dispatches events to the first match.

    Renderers are registered with an integer *priority* (lower values are
    checked first).  When :meth:`render` is called the pipeline builds a
    frozen :class:`RenderingContext` and walks renderers in priority order
    until one returns ``True`` from :meth:`Renderer.can_render`, then
    delegates to that renderer's :meth:`Renderer.render`.

    **Platform registry**

    The pipeline maintains an optional ``adapter_platforms`` mapping from
    adapter ID to platform name (e.g. ``"local-radio"`` → ``"radio-alpha"``).
    When populated, the pipeline passes the platform to each renderer's
    context, enabling platform-aware dispatch.

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
        renderer's context so that renderers can match on platform identity
        rather than adapter-name heuristics.

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
        max_text_chars: int | None = None,
        max_text_bytes: int | None = None,
        delivery_strategy: DeliveryStrategyMethod | None = None,
    ) -> RenderingResult:
        """Try renderers in priority order until one can render.

        Builds a frozen :class:`RenderingContext` from the parameters and
        passes it to each renderer's ``can_render`` and ``render`` methods.
        Renderers decide based on the context; the pipeline does not
        select or bypass renderers based on ``delivery_strategy``.

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
            its internal registry; if still unknown, ``None`` is used.
        max_text_chars:
            Optional maximum text length in characters from the target
            adapter's capabilities.
        max_text_bytes:
            Optional maximum text length in UTF-8 bytes from the target
            adapter's capabilities.
        delivery_strategy:
            Delivery strategy hint from the delivery plan.  Passed to
            renderers via the context as a rendering hint, **not** used
            for renderer selection.  When ``None``, defaults to
            ``"direct"``.

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
        platform = (
            target_platform
            if target_platform is not None
            else self._adapter_platforms.get(target_adapter)
        )

        # Normalise delivery_strategy: default to "direct" when unset.
        strategy: DeliveryStrategyMethod = (
            "direct" if delivery_strategy is None else delivery_strategy
        )

        ctx = RenderingContext(
            delivery_strategy=strategy,
            target_adapter=target_adapter,
            target_channel=target_channel,
            target_platform=platform,
            max_text_chars=max_text_chars,
            max_text_bytes=max_text_bytes,
        )

        for _pri, _seq, renderer in self._renderers:
            if renderer.can_render(event, ctx):
                return await renderer.render(event, ctx)

        raise ValueError(
            f"No renderer registered for event_kind={event.event_kind!r} "
            f"target_adapter={target_adapter!r}"
        )
