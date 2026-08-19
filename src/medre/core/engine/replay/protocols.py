"""Pipeline protocols for replay engine collaboration.

The replay engine uses structural subtyping (Protocol) to describe the
minimal interface it expects from pipeline collaborators.  Two protocols
are defined:

* ``_RealPipelineProtocol`` — methods present on the production
  :class:`~medre.core.engine.pipeline.runner.PipelineRunner`.
* ``_StubPipelineProtocol`` — methods used by test / dummy pipelines
  that do not return :class:`DeliveryPlan` objects from
  ``route_event``.

The replay engine distinguishes production and stub collaborators through
explicit method lookup rather than ``isinstance`` checks, so
``@runtime_checkable`` is intentionally omitted.  Rendering prefers a callable
``render_replay_event`` method, falls back to a callable legacy ``render_event``
method, and reports an error when neither is explicitly implemented.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent


# ---------------------------------------------------------------------------
# Real (production) pipeline protocol
# ---------------------------------------------------------------------------


class _RealPipelineProtocol(Protocol):
    """Methods supplied by the production PipelineRunner.

    The replay engine calls only the methods it needs for the requested
    replay mode. ``transform_event``, ``render_replay_event``, and the legacy
    ``render_event`` fallback are optional at runtime and are invoked only when
    explicit callable implementations are present.
    """

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> tuple[CanonicalEvent, list[tuple[Any, Any]]]:
        """Match *event* against current routes and resolve targets.

        Returns a tuple of (enriched_event, deliveries) where deliveries
        is a list of ``(route, plan)`` pairs with real
        :class:`DeliveryPlan` objects.
        """
        ...

    async def deliver_to_targets(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Any, Any]],
        *,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> list[Any]:
        """Deliver *event* to every target and return outcomes.

        Each target is attempted independently; one target's failure
        never prevents delivery to sibling targets.
        """
        ...

    async def transform_event(self, event: CanonicalEvent) -> CanonicalEvent:
        """Apply registered transforms to *event* and return the result."""
        ...

    async def render_replay_event(self, event: CanonicalEvent) -> Any:
        """Re-render using persisted historical target context."""
        ...

    async def render_event(self, event: CanonicalEvent) -> Any:
        """Render *event* for delivery and return the rendering result."""
        ...


# ---------------------------------------------------------------------------
# Stub / test pipeline protocol
# ---------------------------------------------------------------------------


class _StubPipelineProtocol(Protocol):
    """Methods used by test stub pipelines.

    Stub pipelines do not return :class:`DeliveryPlan` objects from
    ``route_event``; instead they use a separate ``plan_delivery`` step
    and ``deliver`` for execution.
    """

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> tuple[CanonicalEvent, list[tuple[Any, Any]]]:
        """Match *event* against routes, returning (event, pairs)."""
        ...

    async def plan_delivery(
        self,
        event: CanonicalEvent,
        routes: list[tuple[Any, list[Any]]],
    ) -> list[Any]:
        """Build delivery plans for the given event and route-target pairs."""
        ...

    async def deliver(
        self,
        event: CanonicalEvent,
        plans: list[Any],
    ) -> list[Any]:
        """Execute delivery plans and return receipts."""
        ...
