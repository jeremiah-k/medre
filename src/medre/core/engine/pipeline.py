"""Pipeline runner that orchestrates the full event lifecycle.

This module provides the central orchestration engine that wires together
the framework's subsystems into a coherent processing pipeline:

* :class:`PipelineConfig` – configuration dataclass wiring all dependencies.
* :class:`PipelineRunner` – async pipeline that processes events from
  ingress through storage, routing, planning, delivery, and receipt
  recording.

Pipeline stages
---------------
1. **Ingress** – validate and accept an inbound event.
2. **Store** – persist the event via the storage backend.
3. **Route** – match the event against registered routes.
4. **Plan** – create a :class:`DeliveryPlan` for each route target,
   resolving capability fallbacks.
5. **Deliver** – hand the event to the target adapter.
6. **Receipt** – record a :class:`DeliveryReceipt` and store the native
   message mapping.
"""

from __future__ import annotations

import msgspec
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Awaitable, Literal

from medre.adapters.base import AdapterCapabilities, AdapterDeliveryResult, BaseAdapter
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
    NativeRef,
)
from medre.core.events.bus import EventBus, EventMiddleware
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    RetryExecutor,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.backend import StorageBackend

if TYPE_CHECKING:
    from medre.runtime.capacity import CapacityController


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline config
# ---------------------------------------------------------------------------


@dataclass
class PipelineConfig:
    """Configuration bundle wiring all pipeline dependencies.

    Attributes
    ----------
    storage:
        The storage backend for persisting events, receipts, and native refs.
    router:
        The routing engine that matches events to routes.
    fallback_resolver:
        Resolver that downgrades delivery plans when adapters lack
        capability support.
    relation_resolver:
        Resolver for cross-adapter event relation linking.
    adapters:
        Mapping of adapter ID to adapter instance.
    event_bus:
        The event bus used for internal event distribution.
    rendering_pipeline:
        The rendering pipeline that converts :class:`CanonicalEvent`
        into :class:`RenderingResult` before adapter delivery.  If
        ``None``, a default pipeline with a :class:`TextRenderer` is
        created automatically by :class:`PipelineRunner`.
    diagnostician:
        Diagnostic recorder for failure and replay events.  If ``None``,
        a default :class:`Diagnostician` is created automatically.
    logger:
        Optional logger override; defaults to the module logger.
    """

    storage: StorageBackend
    router: Router
    fallback_resolver: FallbackResolver
    relation_resolver: RelationResolver
    adapters: dict[str, BaseAdapter]
    event_bus: EventBus
    rendering_pipeline: RenderingPipeline | None = None
    diagnostician: Diagnostician | None = None
    logger: logging.Logger | None = None
    route_stats: RouteStats | None = None


# ---------------------------------------------------------------------------
# Pipeline middleware (registered with EventBus on start)
# ---------------------------------------------------------------------------


class _PipelineLoggingMiddleware:
    """Internal middleware that logs every event passing through the bus."""

    async def process(self, event: CanonicalEvent) -> CanonicalEvent:
        _logger.debug(
            "Pipeline middleware: event_id=%s kind=%s",
            event.event_id,
            event.event_kind,
        )
        return event


class _AdapterDeliveryError(Exception):
    """Raised by ``deliver_to_target`` after persisting a failed receipt.

    Carries the adapter ID, error string, the original exception, and
    an optional pre-classified ``failure_kind`` so that callers can
    produce a deterministic :class:`DeliveryOutcome` without re-inspecting
    the exception type.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        original: Exception | None = None,
        *,
        failure_kind: DeliveryFailureKind | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.original = original
        self.failure_kind = failure_kind
        super().__init__(error)


class _RendererDeliveryError(Exception):
    """Raised by ``deliver_to_target`` when rendering fails before delivery.

    Carries the adapter ID and error string so callers can produce a
    deterministic :class:`DeliveryOutcome`.
    """

    def __init__(self, adapter_id: str, error: str) -> None:
        self.adapter_id = adapter_id
        self.error = error
        super().__init__(error)


def _default_rendering_pipeline() -> RenderingPipeline:
    """Build a :class:`RenderingPipeline` with a :class:`TextRenderer`.

    Used as the default when :attr:`PipelineConfig.rendering_pipeline` is
    ``None`` so that tests and runtime both get a working renderer
    without explicit wiring.
    """
    pipeline = RenderingPipeline()
    pipeline.register(TextRenderer(), priority=100)
    return pipeline


# ---------------------------------------------------------------------------
# PipelineRunner
# ---------------------------------------------------------------------------


class PipelineRunner:
    """Orchestrates the full event pipeline:

    ingress → store → route → plan → deliver → receipt.

    The runner is started and stopped via :meth:`start` and :meth:`stop`.
    Adapters publish events into the pipeline by calling the
    :attr:`ingress_handler` callable (which is wired into
    :class:`AdapterContext.publish_inbound`).

    Error isolation
    ~~~~~~~~~~~~~~~
    Each delivery target is processed independently.  A failure in one
    target does not prevent delivery to other targets.

    Example
    -------
    >>> config = PipelineConfig(
    ...     storage=storage,
    ...     router=router,
    ...     fallback_resolver=FallbackResolver(),
    ...     relation_resolver=RelationResolver(storage=storage),
    ...     adapters={"discord": adapter},
    ...     event_bus=EventBus(),
    ... )
    >>> runner = PipelineRunner(config)
    >>> await runner.start()
    >>> # Wire runner.ingress_handler into AdapterContext.publish_inbound
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._log: logging.Logger = config.logger or _logger
        self._diagnostician: Diagnostician = (
            config.diagnostician or Diagnostician()
        )
        self._rendering_pipeline: RenderingPipeline = (
            config.rendering_pipeline or _default_rendering_pipeline()
        )
        self._middleware: _PipelineLoggingMiddleware | None = None
        self._route_stats: RouteStats | None = config.route_stats
        self._capacity_controller: CapacityController | None = None

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Register pipeline middleware with the event bus.

        Call this before any adapter calls :attr:`ingress_handler`.

        On startup the runner populates the rendering pipeline's platform
        registry from the configured adapters so that renderer selection
        can use platform identity rather than adapter-name heuristics.
        """
        self._middleware = _PipelineLoggingMiddleware()
        self._config.event_bus.add_middleware(self._middleware, priority=100)

        # Populate the rendering pipeline's platform registry from the
        # configured adapters so that transport-specific renderers can
        # match on platform identity instead of adapter-name prefixes
        # or ad-hoc known-adapters sets.
        self._populate_renderer_platforms()

        self._log.info("PipelineRunner started")

    def _populate_renderer_platforms(self) -> None:
        """Register each adapter's platform with the rendering pipeline."""
        platforms: dict[str, str] = {}
        for adapter_id, adapter in self._config.adapters.items():
            platform = getattr(adapter, "platform", None)
            if platform and isinstance(platform, str):
                platforms[adapter_id] = platform
        if platforms:
            self._rendering_pipeline.register_platforms_from(platforms)
            self._log.debug(
                "Populated rendering pipeline platform registry: %s", platforms
            )

    def set_capacity_controller(self, cc: CapacityController) -> None:
        """Wire a :class:`~medre.runtime.capacity.CapacityController`.

        When set, :meth:`deliver_to_targets` acquires a delivery slot
        before processing and releases it on completion.
        """
        self._capacity_controller = cc

    async def stop(self) -> None:
        """Remove pipeline middleware from the event bus.

        Safe to call even if :meth:`start` was never called.
        """
        if self._middleware is not None:
            self._config.event_bus.remove_middleware(self._middleware)
            self._middleware = None
        self._log.info("PipelineRunner stopped")

    # -- Ingress -----------------------------------------------------------

    @property
    def ingress_handler(self):
        """Return a callable suitable for ``AdapterContext.publish_inbound``.

        The returned coroutine function accepts a single
        :class:`CanonicalEvent` and feeds it into the pipeline.
        """
        return self.handle_ingress

    async def handle_ingress(
        self, event: CanonicalEvent
    ) -> list[DeliveryOutcome]:
        """Process an inbound event through the full pipeline.

        Flow:

        1. Validate required fields.
        2. Resolve relations (native refs → canonical event IDs).
        3. Store the event.
        4. Persist inbound native ref (if source_native_ref is present).
        5. Route the event and create delivery plans.
        6. Deliver to each target independently.

        Parameters
        ----------
        event:
            The canonical event to process.

        Returns
        -------
        list[DeliveryOutcome]
            Per-target delivery outcomes.  Empty when no routes matched.
        """
        self._log.info(
            "Ingress: event_id=%s kind=%s source=%s",
            event.event_id,
            event.event_kind,
            event.source_adapter,
        )

        # Stage 1 – validate
        self._validate_event(event)

        # Stage 2 – resolve relations (pipeline-owned, not adapter/codec).
        event = await self._resolve_relations(event)

        # Stage 3 – store
        await self.store_event(event)

        # Stage 4 – persist inbound native ref
        await self._persist_inbound_native_ref(event)

        # Stages 5-6 – route, plan, deliver
        try:
            event, deliveries = await self.route_event(event)
        except Exception as exc:
            self._diagnostician.record_planner_failure(
                event.event_id, f"{type(exc).__name__}: {exc}"
            )
            return [
                DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter="",
                    target_channel=None,
                    route_id="",
                    delivery_plan_id="",
                    status="permanent_failure",
                    failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
                    receipt=None,
                    error=f"Planner error: {type(exc).__name__}: {exc}",
                    duration_ms=0.0,
                )
            ]

        if not deliveries:
            self._log.info(
                "No routes matched for event_id=%s", event.event_id
            )
            return []

        # Deliver to all targets independently with error isolation.
        outcomes = await self.deliver_to_targets(event, deliveries)

        succeeded = sum(1 for o in outcomes if o.status == "success")
        failed = len(outcomes) - succeeded
        self._log.info(
            "Pipeline complete: event_id=%s targets=%d succeeded=%d failed=%d",
            event.event_id,
            len(deliveries),
            succeeded,
            failed,
        )

        return outcomes

    # -- Stage 1: Validation -----------------------------------------------

    @staticmethod
    def _validate_event(event: CanonicalEvent) -> None:
        """Validate that *event* has all required fields.

        Raises
        ------
        ValueError
            If any required field is missing or empty.
        """
        if not event.event_id:
            raise ValueError("Event must have a non-empty event_id")
        if not event.event_kind:
            raise ValueError("Event must have a non-empty event_kind")
        if not event.source_adapter:
            raise ValueError("Event must have a non-empty source_adapter")

    # -- Stage 2: Storage --------------------------------------------------

    async def store_event(self, event: CanonicalEvent) -> None:
        """Persist *event* to the storage backend.

        Parameters
        ----------
        event:
            The canonical event to store.
        """
        self._log.debug(
            "Storing event: event_id=%s kind=%s",
            event.event_id,
            event.event_kind,
        )
        await self._config.storage.append(event)

    # -- Stage 2: Relation resolution ------------------------------------

    async def _resolve_relations(
        self, event: CanonicalEvent
    ) -> CanonicalEvent:
        """Resolve event-level relations using the relation resolver.

        Delegates to :class:`RelationResolver` to look up
        ``target_native_ref`` → ``target_event_id`` mappings.  Unresolved
        native refs are preserved.  Returns the original event when no
        changes are needed; returns a new (immutable) event otherwise.
        """
        return await self._config.relation_resolver.resolve_event_relations(event)

    # -- Stage 4: Inbound native ref persistence -------------------------

    async def _persist_inbound_native_ref(
        self, event: CanonicalEvent
    ) -> None:
        """Persist an inbound native ref when ``source_native_ref`` exists.

        Creates a :class:`NativeMessageRef` with ``direction="inbound"``
        mapping the source native ref fields to the canonical ``event_id``.
        Idempotent: duplicate ``(adapter, native_channel_id,
        native_message_id)`` triples are silently ignored by the storage
        layer.
        """
        snr = event.source_native_ref
        if snr is None or not snr.native_message_id:
            return

        now = datetime.now(tz=timezone.utc)
        inbound_ref = NativeMessageRef(
            id=f"nref-inbound-{uuid.uuid4()}",
            event_id=event.event_id,
            adapter=snr.adapter,
            native_channel_id=snr.native_channel_id,
            native_message_id=snr.native_message_id,
            native_thread_id=snr.native_thread_id,
            native_relation_id=None,
            direction="inbound",
            created_at=now,
        )
        await self._config.storage.store_native_ref(inbound_ref)

    # -- Stage 3-4: Routing + Planning -------------------------------------

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> tuple[CanonicalEvent, list[tuple[Route, DeliveryPlan]]]:
        """Match *event* against routes and produce delivery plans.

        For each matched route, resolves its targets and creates a
        :class:`DeliveryPlan` per target using the fallback resolver.
        Populates :attr:`RoutingMetadata.route_trace` on the returned
        event with the matched route IDs.

        Parameters
        ----------
        event:
            The canonical event to route.

        Returns
        -------
        tuple[CanonicalEvent, list[tuple[Route, DeliveryPlan]]]
            The event (with route_trace populated) and paired routes
            with their per-target delivery plans.
        """
        matched_routes = self._config.router.match(event)

        if not matched_routes:
            self._log.debug(
                "No routes matched for event_id=%s kind=%s",
                event.event_id,
                event.event_kind,
            )
            return event, []

        # Populate matched_routes and route_trace on the event's routing metadata.
        route_ids = tuple(r.id for r in matched_routes)
        existing_routing = event.metadata.routing
        # Build the new route_trace by appending current route IDs to
        # the existing trace, bounded to at most 16 entries.
        prior_trace: tuple[str, ...] = ()
        if existing_routing is not None:
            prior_trace = existing_routing.route_trace if existing_routing.route_trace else ()
        new_trace = (prior_trace + route_ids)[-16:]
        if existing_routing is not None:
            new_routing = msgspec.structs.replace(
                existing_routing,
                matched_routes=route_ids,
                route_trace=new_trace,
            )
        else:
            from medre.core.events.metadata import RoutingMetadata
            new_routing = RoutingMetadata(
                matched_routes=route_ids, route_trace=new_trace,
            )
        new_metadata = msgspec.structs.replace(
            event.metadata, routing=new_routing,
        )
        event = msgspec.structs.replace(event, metadata=new_metadata)

        results: list[tuple[Route, DeliveryPlan]] = []

        for route in matched_routes:
            targets = self._config.router.resolve_targets(event, route)

            for target in targets:
                capabilities = self._get_adapter_capabilities(target)
                plan = self._config.fallback_resolver.resolve_fallback(
                    event, target, capabilities,
                )
                results.append((route, plan))
                self._log.debug(
                    "Planned delivery: route=%s target_adapter=%s plan=%s",
                    route.id,
                    target.adapter,
                    plan.plan_id,
                )

        return event, results

    # -- Stage 5-6: Delivery + Receipts ------------------------------------

    async def deliver_to_targets(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Route, DeliveryPlan]],
    ) -> list[DeliveryOutcome]:
        """Deliver *event* to every target and return categorised outcomes.

        Each target is attempted independently; one target's failure never
        prevents delivery to sibling targets.  Adapter errors are
        classified as transient or permanent based on exception type, and
        every failure is recorded via the :class:`Diagnostician`.

        Parameters
        ----------
        event:
            The canonical event to deliver.
        route_targets:
            Paired routes and their per-target delivery plans, as
            returned by :meth:`route_event`.

        Returns
        -------
        list[DeliveryOutcome]
            One :class:`DeliveryOutcome` per target, preserving the
            order of *route_targets*.
        """
        # Capacity guard: acquire a delivery slot before processing.
        if self._capacity_controller is not None:
            acquired = await self._capacity_controller.acquire_delivery()
            if not acquired:
                return [
                    DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=(
                            p.target.adapter if hasattr(p, "target") else ""
                        ),
                        target_channel=(
                            p.target.channel if hasattr(p, "target") else None
                        ),
                        route_id=r.id,
                        delivery_plan_id=(
                            p.plan_id if hasattr(p, "plan_id") else ""
                        ),
                        status="permanent_failure",
                        failure_kind=None,
                        receipt=None,
                        error="delivery_capacity_exceeded",
                        duration_ms=0.0,
                    )
                    for r, p in route_targets
                ]
        try:
            return await self._deliver_to_targets_inner(event, route_targets)
        finally:
            if self._capacity_controller is not None:
                await self._capacity_controller.release_delivery()

    async def _deliver_to_targets_inner(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Route, DeliveryPlan]],
    ) -> list[DeliveryOutcome]:

        async def _deliver_one(
            route: Route, plan: DeliveryPlan
        ) -> DeliveryOutcome:
            target = plan.target
            adapter_id = target.adapter or ""
            t0 = time.monotonic()

            # Route-trace loop prevention: skip if this route has already
            # been traversed in a *prior* routing pass.  The first occurrence
            # of a route ID in the trace is the current pass — allow it.
            # A second or later occurrence means the event was re-routed
            # through the same route (e.g. during replay or multi-hop
            # topologies).
            routing_meta = event.metadata.routing
            trace_count = 0
            if routing_meta is not None:
                trace_count = sum(1 for tid in routing_meta.route_trace if tid == route.id)
            if trace_count > 1:
                self._log.warning(
                    "loop_prevented: route_id=%s already in route_trace "
                    "for event_id=%s (trace=%s)",
                    route.id,
                    event.event_id,
                    routing_meta.route_trace,
                )
                if self._route_stats is not None:
                    self._route_stats.record_loop_prevented(route.id)
                elapsed = (time.monotonic() - t0) * 1000.0
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="skipped",
                    failure_kind=None,
                    receipt=None,
                    error="loop_prevented: route already traversed in prior routing pass",
                    duration_ms=elapsed,
                )

            # Self-loop guard: skip delivery back to the source adapter.
            if adapter_id and adapter_id == event.source_adapter:
                self._log.warning(
                    "loop_prevented: skipping delivery of event_id=%s "
                    "back to source_adapter=%s (route=%s)",
                    event.event_id,
                    adapter_id,
                    route.id,
                )
                if self._route_stats is not None:
                    self._route_stats.record_loop_prevented(route.id)
                elapsed = (time.monotonic() - t0) * 1000.0
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="skipped",
                    failure_kind=None,
                    receipt=None,
                    error="loop_prevented",
                    duration_ms=elapsed,
                )

            try:
                receipt = await self.deliver_to_target(event, route, plan)
                elapsed = (time.monotonic() - t0) * 1000.0
                if self._route_stats is not None:
                    self._route_stats.record_delivered(route.id)
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="success",
                    failure_kind=None,
                    receipt=receipt,
                    error=None,
                    duration_ms=elapsed,
                )
            except _AdapterDeliveryError as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                self._diagnostician.record_adapter_failure(
                    event.event_id, adapter_id, exc.error
                )
                if self._route_stats is not None:
                    self._route_stats.record_failed(route.id, exc.error)
                # Use pre-classified failure_kind when available (e.g.
                # TARGET_NOT_FOUND, DEADLINE_EXCEEDED); otherwise classify
                # based on the original adapter exception.
                if exc.failure_kind is not None:
                    failure_kind = exc.failure_kind
                elif exc.original is not None:
                    failure_kind = RetryExecutor.classify_failure(
                        exc.original,
                        adapter_registered=True,
                    )
                else:
                    failure_kind = DeliveryFailureKind.ADAPTER_TRANSIENT
                outcome_status: Literal[
                    "transient_failure", "permanent_failure"
                ] = (
                    "transient_failure"
                    if failure_kind.is_retryable
                    else "permanent_failure"
                )
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status=outcome_status,
                    failure_kind=failure_kind,
                    receipt=None,
                    error=exc.error,
                    duration_ms=elapsed,
                )
            except _RendererDeliveryError as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                if self._route_stats is not None:
                    self._route_stats.record_failed(route.id, exc.error)
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="permanent_failure",
                    failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
                    receipt=None,
                    error=exc.error,
                    duration_ms=elapsed,
                )
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                exc_type = type(exc)
                failure_kind = RetryExecutor.classify_failure(
                    exc,
                    adapter_registered=(adapter_id in self._config.adapters),
                )
                status = (
                    "transient_failure"
                    if failure_kind.is_retryable
                    else "permanent_failure"
                )
                error_msg = f"{exc_type.__name__}: {exc}"
                self._diagnostician.record_adapter_failure(
                    event.event_id, adapter_id, error_msg
                )
                if self._route_stats is not None:
                    self._route_stats.record_failed(route.id, error_msg)
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status=status,
                    failure_kind=failure_kind,
                    receipt=None,
                    error=error_msg,
                    duration_ms=elapsed,
                )

        return list(
            await asyncio.gather(
                *[_deliver_one(r, p) for r, p in route_targets]
            )
        )

    @staticmethod
    def _classify_adapter_error(
        exc: Exception,
    ) -> Literal["transient_failure", "permanent_failure"]:
        """Classify an adapter exception as transient or permanent.

        Uses the :class:`RetryExecutor.classify_failure` taxonomy to
        determine retryability.  Transient failures are retryable
        (timeouts, connection errors, temporary OS-level issues).
        All other exceptions are treated as permanent failures.

        Parameters
        ----------
        exc:
            The exception raised by the adapter.

        Returns
        -------
        str
            ``"transient_failure"`` or ``"permanent_failure"``.
        """
        kind = RetryExecutor.classify_failure(exc)
        return (
            "transient_failure" if kind.is_retryable else "permanent_failure"
        )

    async def deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        previous_receipt: DeliveryReceipt | None = None,
    ) -> DeliveryReceipt:
        """Deliver *event* to a single target adapter and record the receipt.

        Steps:

        1. Look up the target adapter from the config.
        2. Render the event via the rendering pipeline.
        3. Call the adapter's ``deliver`` method.
        4. Record a :class:`DeliveryReceipt` in storage with receipt
           lineage (``attempt_number``, ``parent_receipt_id``).
        5. Store a :class:`NativeMessageRef` mapping.
        6. If the delivery fails and a :class:`RetryPolicy` is configured,
           compute the next retry state.  If retries are exhausted, record
           a ``dead_lettered`` receipt.

        **Phase 1 does not implement a background retry scheduler.**
        Retry is synchronous/receipt-level only: this method records the
        failure receipt with ``next_retry_at`` populated.  A future
        scheduler or manual replay re-invokes this method with the
        ``previous_receipt`` parameter.

        Parameters
        ----------
        event:
            The canonical event to deliver.
        route:
            The route that matched the event.
        plan:
            The delivery plan for this target.
        previous_receipt:
            The receipt from the previous delivery attempt, if this is a
            retry.  ``None`` for the first attempt.

        Returns
        -------
        DeliveryReceipt
            The receipt recording the delivery outcome.
        """
        target = plan.target
        adapter_id = target.adapter
        now = datetime.now(tz=timezone.utc)
        receipt_id = f"rcpt-{uuid.uuid4()}"

        # Compute attempt number and parent receipt for lineage.
        attempt_number = 1
        parent_receipt_id: str | None = None
        if previous_receipt is not None:
            attempt_number = previous_receipt.attempt_number + 1
            parent_receipt_id = previous_receipt.receipt_id

        adapter = self._config.adapters.get(adapter_id) if adapter_id else None

        if adapter is None:
            self._log.warning(
                "Target adapter %r not found; event_id=%s",
                adapter_id,
                event.event_id,
            )
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                route_id=route.id,
                status="failed",
                error=f"Adapter {adapter_id!r} not registered",
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
            )
            await self._config.storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                f"Adapter {adapter_id!r} not registered",
                failure_kind=DeliveryFailureKind.TARGET_NOT_FOUND,
            ) from None

        # Check delivery plan deadline.
        if plan.deadline is not None and now > plan.deadline:
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                route_id=route.id,
                status="failed",
                error="Delivery deadline exceeded",
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
            )
            await self._config.storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                "Delivery deadline exceeded",
                failure_kind=DeliveryFailureKind.DEADLINE_EXCEEDED,
            ) from None

        # Render the event into a RenderingResult before adapter delivery.
        # Pass the adapter's platform so renderers can match on platform
        # identity instead of adapter-name heuristics.
        target_platform = getattr(adapter, "platform", None)
        if isinstance(target_platform, str):
            platform_param: str | None = target_platform
        else:
            platform_param = None
        try:
            rendering_result = await self._rendering_pipeline.render(
                event, adapter_id or "", target.channel,
                target_platform=platform_param,
            )
        except Exception as exc:
            rendering_error = f"Rendering failed: {type(exc).__name__}: {exc}"
            self._diagnostician.record_renderer_failure(
                event.event_id, adapter_id or "", rendering_error
            )
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                route_id=route.id,
                status="failed",
                error=rendering_error,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
            )
            await self._config.storage.append_receipt(receipt)
            raise _RendererDeliveryError(adapter_id or "", rendering_error) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        adapter_result: AdapterDeliveryResult | None = None
        try:
            deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
            if deliver_fn is not None and callable(deliver_fn):
                adapter_result = await deliver_fn(rendering_result)
            else:
                self._log.warning(
                    "Adapter %r has no deliver() method; skipping delivery",
                    adapter_id,
                )

            status: Literal["sent", "failed"] = "sent"
            error: str | None = None
            self._log.info(
                "Delivered: event_id=%s → adapter=%s plan=%s attempt=%d",
                event.event_id,
                adapter_id,
                plan.plan_id,
                attempt_number,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            delivery_exc = exc
            self._log.exception(
                "Delivery failed: event_id=%s → adapter=%s attempt=%d",
                event.event_id,
                adapter_id,
                attempt_number,
            )

        # Determine if we need to record a retry or dead-letter receipt.
        # This happens AFTER the main receipt is persisted (below) to
        # maintain correct append ordering. We capture the decision here
        # and execute after the primary receipt.
        _needs_dead_letter = (
            status == "failed"
            and plan.retry_policy is not None
            and RetryExecutor(plan.retry_policy).is_exhausted(attempt_number)
        )

        # Record receipt.
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id or "",
            route_id=route.id,
            status=status,
            error=error,
            created_at=now,
            attempt_number=attempt_number,
            parent_receipt_id=parent_receipt_id,
        )
        await self._config.storage.append_receipt(receipt)

        # If all retries exhausted, append dead-letter receipt after
        # the primary receipt to maintain append-only ordering.
        if _needs_dead_letter and plan.retry_policy is not None:
            executor = RetryExecutor(plan.retry_policy)
            dead_receipt = executor.build_dead_letter_receipt(
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                previous_receipt_id=receipt_id,
                attempt_number=attempt_number + 1,
                error=error or "Retry exhausted",
            )
            await self._config.storage.append_receipt(dead_receipt)

        # Store native ref mapping (outbound direction) ONLY on success.
        # Use adapter-provided native IDs; never fabricate synthetic IDs.
        if status == "sent" and adapter_result is not None and adapter_result.native_message_id is not None:
            native_ref = NativeMessageRef(
                id=f"nref-{uuid.uuid4()}",
                event_id=event.event_id,
                adapter=adapter_id or "",
                native_channel_id=adapter_result.native_channel_id or target.channel,
                native_message_id=adapter_result.native_message_id,
                native_thread_id=adapter_result.native_thread_id,
                native_relation_id=adapter_result.native_relation_id,
                direction="outbound",
                created_at=now,
            )
            await self._config.storage.store_native_ref(native_ref)

        # Re-raise adapter errors so that callers (deliver_to_targets)
        # can inspect the exception type for transient/permanent classification.
        # The receipt and native ref are already persisted at this point.
        if status == "failed":
            raise _AdapterDeliveryError(
                adapter_id or "", error or "", delivery_exc
            ) from None

        return receipt

    # -- Internal helpers --------------------------------------------------

    async def _deliver_all(
        self,
        event: CanonicalEvent,
        deliveries: list[tuple[Route, DeliveryPlan]],
    ) -> list[DeliveryReceipt | None]:
        """Deliver to all targets concurrently with error isolation.

        Returns a list parallel to *deliveries*; ``None`` entries indicate
        a target that raised an unhandled exception.
        """
        async def _safe_deliver(
            route: Route, plan: DeliveryPlan
        ) -> DeliveryReceipt | None:
            try:
                return await self.deliver_to_target(event, route, plan)
            except Exception:
                self._log.exception(
                    "Unhandled error delivering event_id=%s to adapter=%s",
                    event.event_id,
                    plan.target.adapter,
                )
                return None

        return list(
            await asyncio.gather(
                *[_safe_deliver(r, p) for r, p in deliveries]
            )
        )

    def _get_adapter_capabilities(self, target: RouteTarget) -> dict:
        """Retrieve the capabilities dict for a target adapter.

        Returns an empty dict if the adapter is not found or does not
        report capabilities.
        """
        adapter_id = target.adapter
        if adapter_id is None:
            return {}

        adapter = self._config.adapters.get(adapter_id)
        if adapter is None:
            return {}

        # Try to get capabilities from health_check result; fall back to
        # looking for a capabilities attribute directly.
        if hasattr(adapter, "_capabilities"):
            caps = adapter._capabilities  # type: ignore[attr-defined]
            if isinstance(caps, AdapterCapabilities):
                return self._caps_to_dict(caps)

        return {}

    @staticmethod
    def _caps_to_dict(caps: AdapterCapabilities) -> dict:
        """Convert an :class:`AdapterCapabilities` to a plain dict.

        Maps capability fields to the keys expected by
        :class:`FallbackResolver`.
        """
        return {
            "supports_reactions": caps.reactions != "unsupported",
            "supports_edits": caps.edits != "unsupported",
            "supports_deletes": caps.deletes != "unsupported",
            "text": caps.text,
            "replies": caps.replies,
            "reactions": caps.reactions,
            "edits": caps.edits,
        }
