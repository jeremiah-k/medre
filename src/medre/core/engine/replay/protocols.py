"""Pipeline and event-bus protocols for replay engine collaboration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent


# ---------------------------------------------------------------------------
# Pipeline protocol (optional collaborator)
# ---------------------------------------------------------------------------


@runtime_checkable
class _PipelineProtocol(Protocol):
    """Minimal protocol that the pipeline collaborator must satisfy.

    The replay engine only calls the methods it needs for the requested
    replay mode.  If a method is not needed (e.g. ``deliver`` in STRICT
    mode), the pipeline does not have to provide it.

    Methods
    -------
    transform_event:
        Apply registered transforms to an event.
    render_event:
        Render an event for delivery.
    route_event:
        Match an event against current routes and resolve targets.
    plan_delivery:
        Build delivery plans from routing results.  (Stub pipelines only;
        real pipelines return plans directly from ``route_event``.)
    deliver:
        Execute delivery plans to adapters.  (Stub pipelines only; real
        pipelines use ``deliver_to_targets`` instead.)
    deliver_to_targets:
        Deliver an event to route--plan pairs.  (Real PipelineRunner only;
        stub pipelines use ``deliver`` instead.)
    """

    async def transform_event(self, event: CanonicalEvent) -> CanonicalEvent:
        """Apply registered transforms to *event* and return the result."""
        ...

    async def render_event(self, event: CanonicalEvent) -> Any:
        """Render *event* for delivery and return the rendering result."""
        ...

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> tuple[CanonicalEvent, list[tuple[Any, Any]]]:
        """Match *event* against current routes and resolve targets.

        Returns a tuple of (enriched_event, deliveries) where deliveries
        is a list of ``(route, plan)`` pairs.
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

    # -- Stub-pipeline methods (not on real PipelineRunner) ----------------
    # These are kept so that test stub pipelines that only implement
    # ``plan_delivery`` / ``deliver`` continue to work.  The replay
    # engine uses ``hasattr`` detection to branch between real and
    # stub pipelines at runtime.

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


@runtime_checkable
class _EventBusProtocol(Protocol):
    """Minimal event-bus protocol for publishing replayed events.

    Accepted by :class:`ReplayEngine` but not invoked during replay.
    Reserved for future notification use.
    """

    async def publish(
        self,
        event: CanonicalEvent,
        *,
        source: str = "",
    ) -> None:
        """Publish *event* to the bus."""
        ...
