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

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, cast

import msgspec

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContract,
    AdapterDeliveryResult,
)
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    EventRelation,
    NativeMessageRef,
    NativeRef,
)
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
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
    route_retry_policies:
        Mapping from expanded route ID to :class:`RetryPolicy`.  When a
        route is matched and its expanded ID is in this dict, the policy
        is attached to the :class:`DeliveryPlan` so transient failures
        produce retry receipts.
    """

    storage: StorageBackend
    router: Router
    fallback_resolver: FallbackResolver
    relation_resolver: RelationResolver
    adapters: dict[str, AdapterContract]
    event_bus: EventBus
    rendering_pipeline: RenderingPipeline | None = None
    diagnostician: Diagnostician | None = None
    logger: logging.Logger | None = None
    route_stats: RouteStats | None = None
    runtime_accounting: RuntimeAccounting | None = None
    route_retry_policies: dict[str, RetryPolicy] = field(default_factory=dict)


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


def _native_metadata_for_ref(event: CanonicalEvent) -> dict[str, object]:
    """Extract native metadata dict from *event* without mutation.

    Returns ``dict(event.metadata.native.data)`` when native metadata is
    present, otherwise an empty dict.  The returned dict is a plain
    mutable copy suitable for passing to :class:`NativeMessageRef`.
    """
    native = event.metadata.native
    if native is not None and native.data:
        return dict(native.data)
    return {}


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
        self._diagnostician: Diagnostician = config.diagnostician or Diagnostician()
        self._rendering_pipeline: RenderingPipeline = (
            config.rendering_pipeline or _default_rendering_pipeline()
        )
        self._middleware: _PipelineLoggingMiddleware | None = None
        self._route_stats: RouteStats | None = config.route_stats
        self._runtime_accounting: RuntimeAccounting | None = config.runtime_accounting
        self._capacity_controller: CapacityController | None = None
        self._delivery_rejection_count: int = 0

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

        When set, each per-target delivery inside :meth:`_deliver_to_targets_inner`
        acquires a delivery slot before processing and releases it on
        completion (success, failure, or skip).
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

    async def handle_ingress(self, event: CanonicalEvent) -> list[DeliveryOutcome]:
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

        # Stage 1.5 – duplicate native ref check.  If this event carries
        # a source_native_ref that already resolves to an existing
        # canonical event, the pipeline has already processed this
        # message.  Skip store + delivery to prevent duplicates and
        # echo loops.
        snr = event.source_native_ref
        if snr is not None and snr.native_message_id:
            existing_event_id = await self._config.storage.resolve_native_ref(
                adapter=snr.adapter,
                native_channel_id=snr.native_channel_id,
                native_message_id=snr.native_message_id,
            )
            if existing_event_id is not None:
                self._log.info(
                    "Duplicate native ref suppressed: event_id=%s "
                    "native_ref=(%s,%s,%s) already mapped to %s",
                    event.event_id,
                    snr.adapter,
                    snr.native_channel_id,
                    snr.native_message_id,
                    existing_event_id,
                )
                if self._runtime_accounting is not None:
                    self._runtime_accounting.record_loop_prevented()
                return []

        # Accounting: inbound event accepted past validation + dedup.
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_inbound_accepted()

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
            # NOTE(semantics): Planner failure produces no
            # DeliveryReceipt because delivery planning itself failed;
            # the event never reached the delivery stage.  The outcome
            # below serves as the in-memory record; durable evidence is
            # via the Diagnostician event log, not delivery_receipts.
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
            self._log.info("No routes matched for event_id=%s", event.event_id)
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

    async def _resolve_relations(self, event: CanonicalEvent) -> CanonicalEvent:
        """Resolve event-level relations using the relation resolver.

        Delegates to :class:`RelationResolver` to look up
        ``target_native_ref`` → ``target_event_id`` mappings.  Unresolved
        native refs are preserved.  Returns the original event when no
        changes are needed; returns a new (immutable) event otherwise.
        """
        return await self._config.relation_resolver.resolve_event_relations(event)

    async def _enrich_relations_for_target(
        self,
        event: CanonicalEvent,
        target_adapter: str,
    ) -> CanonicalEvent:
        """Enrich relations with target-adapter native refs for rendering.

        For each relation that has a ``target_event_id`` but whose
        ``target_native_ref`` is either missing or not for *target_adapter*,
        look up stored native refs for the target event and attach the
        first matching one.  This enables structured replies / reactions
        in target-adapter native ID space.

        Returns a new event when any relation is enriched; returns the
        original event unchanged otherwise.  **Never mutates** the stored
        original event.
        """
        if not event.relations:
            return event

        storage = self._config.storage
        list_fn = getattr(storage, "list_native_refs_for_event", None)
        if not callable(list_fn):
            return event

        changed = False
        new_relations: list[EventRelation] = []

        for rel in event.relations:
            if not rel.target_event_id:
                new_relations.append(rel)
                continue

            # Check if already has a native ref for the target adapter.
            if (
                rel.target_native_ref is not None
                and rel.target_native_ref.adapter == target_adapter
            ):
                new_relations.append(rel)
                continue

            # Look up stored native refs for the target event.
            try:
                list_native_refs = cast(
                    Callable[[str], Awaitable[list[NativeMessageRef]]],
                    list_fn,
                )
                refs = await list_native_refs(rel.target_event_id)
            except Exception:
                self._log.debug(
                    "Failed to enrich relation native ref for "
                    "target_event_id=%s target_adapter=%s relation_type=%s",
                    getattr(rel, "target_event_id", "?"),
                    target_adapter,
                    getattr(rel, "relation_type", "?"),
                    exc_info=True,
                )
                new_relations.append(rel)
                continue

            # Find first ref matching the target adapter.
            matching: NativeMessageRef | None = None
            for nref in refs:
                if nref.adapter == target_adapter:
                    matching = nref
                    break

            if matching is None:
                new_relations.append(rel)
                continue

            # Build enriched native ref.
            enriched_native_ref = NativeRef(
                adapter=matching.adapter,
                native_channel_id=matching.native_channel_id,
                native_message_id=matching.native_message_id,
                native_thread_id=matching.native_thread_id,
            )

            # Build enriched relation preserving original fields.
            enriched_rel = EventRelation(
                relation_type=rel.relation_type,
                target_event_id=rel.target_event_id,
                target_native_ref=enriched_native_ref,
                key=rel.key,
                fallback_text=rel.fallback_text,
                metadata=dict(rel.metadata) if rel.metadata else {},
            )
            new_relations.append(enriched_rel)
            changed = True

        if not changed:
            return event

        return msgspec.structs.replace(event, relations=tuple(new_relations))

    # -- Stage 4: Inbound native ref persistence -------------------------

    async def _persist_inbound_native_ref(self, event: CanonicalEvent) -> None:
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
            metadata=_native_metadata_for_ref(event),
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
            prior_trace = (
                existing_routing.route_trace if existing_routing.route_trace else ()
            )
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
                matched_routes=route_ids,
                route_trace=new_trace,
            )
        new_metadata = msgspec.structs.replace(
            event.metadata,
            routing=new_routing,
        )
        event = msgspec.structs.replace(event, metadata=new_metadata)

        results: list[tuple[Route, DeliveryPlan]] = []

        for route in matched_routes:
            targets = self._config.router.resolve_targets(event, route)

            for target in targets:
                capabilities = self._get_adapter_capabilities(target)
                plan = self._config.fallback_resolver.resolve_fallback(
                    event,
                    target,
                    capabilities,
                )
                # Attach route-level retry policy if configured.
                retry_policy = self._config.route_retry_policies.get(route.id)
                if retry_policy is not None:
                    plan.retry_policy = retry_policy
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
        *,
        source: str = "live",
        replay_run_id: str | None = None,
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
        source:
            Origin of delivery: ``"live"``, ``"retry"``, or ``"replay"``.
        replay_run_id:
            When ``source="replay"``, the replay run identifier.

        Returns
        -------
        list[DeliveryOutcome]
            One :class:`DeliveryOutcome` per target, preserving the
            order of *route_targets*.
        """
        # Per-target capacity acquire/release happens inside _deliver_one().
        return await self._deliver_to_targets_inner(
            event,
            route_targets,
            source=source,
            replay_run_id=replay_run_id,
        )

    async def _deliver_to_targets_inner(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Route, DeliveryPlan]],
        *,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> list[DeliveryOutcome]:

        async def _deliver_one(
            route: Route, route_plan: DeliveryPlan
        ) -> DeliveryOutcome:
            target = route_plan.target
            adapter_id = target.adapter or ""
            t0 = time.monotonic()

            # Per-target capacity guard: acquire a slot before any work.
            if self._capacity_controller is not None:
                acquired = await self._capacity_controller.acquire_delivery()
                if not acquired:
                    self._delivery_rejection_count += 1
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_capacity_rejection()
                    if self._route_stats is not None:
                        self._route_stats.record_failed(
                            route.id, "delivery_capacity_exceeded"
                        )
                    # Classify: shutdown vs capacity exhaustion.
                    if not self._capacity_controller.accepting_work:
                        capacity_failure_kind = DeliveryFailureKind.SHUTDOWN_REJECTION
                        capacity_error = "delivery_rejected_shutdown"
                    else:
                        capacity_failure_kind = DeliveryFailureKind.CAPACITY_REJECTION
                        capacity_error = "delivery_capacity_exceeded"
                    elapsed = (time.monotonic() - t0) * 1000.0
                    # NOTE(semantics): Capacity and shutdown rejections
                    # intentionally produce no persisted DeliveryReceipt.
                    # The event never entered the delivery stage; the
                    # rejection occurs at the capacity gate *before* any
                    # adapter interaction.  Durable evidence of the
                    # rejection is recorded via RuntimeAccounting counters
                    # and RouteStats, NOT via delivery_receipts.
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=(
                            route_plan.plan_id if hasattr(route_plan, "plan_id") else ""
                        ),
                        status="permanent_failure",
                        failure_kind=capacity_failure_kind,
                        receipt=None,
                        error=capacity_error,
                        duration_ms=elapsed,
                    )
            try:
                # Route-trace loop prevention: skip if this route has already
                # been traversed in a *prior* routing pass.  The first occurrence
                # of a route ID in the trace is the current pass — allow it.
                # A second or later occurrence means the event was re-routed
                # through the same route (e.g. during replay or multi-hop
                # topologies).
                routing_meta = event.metadata.routing
                trace_count = 0
                if routing_meta is not None:
                    trace_count = sum(
                        1 for tid in routing_meta.route_trace if tid == route.id
                    )
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
                        if self._runtime_accounting is not None:
                            self._runtime_accounting.record_loop_prevented()
                        elapsed = (time.monotonic() - t0) * 1000.0
                        return DeliveryOutcome(
                            event_id=event.event_id,
                            target_adapter=adapter_id,
                            target_channel=target.channel,
                            route_id=route.id,
                            delivery_plan_id=route_plan.plan_id,
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
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_loop_prevented()
                    elapsed = (time.monotonic() - t0) * 1000.0
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status="skipped",
                        failure_kind=None,
                        receipt=None,
                        error="loop_prevented",
                        duration_ms=elapsed,
                    )

                try:
                    # Accounting: outbound delivery attempt.
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_attempt()
                    receipt = await self.deliver_to_target(
                        event,
                        route,
                        route_plan,
                        source=source,
                        replay_run_id=replay_run_id,
                    )
                    elapsed = (time.monotonic() - t0) * 1000.0
                    if self._route_stats is not None:
                        self._route_stats.record_delivered(route.id)
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_delivered()
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
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
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_failed()
                    # Use pre-classified failure_kind when available (e.g.
                    # ADAPTER_MISSING, DEADLINE_EXCEEDED); otherwise classify
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
                        delivery_plan_id=route_plan.plan_id,
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
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_failed()
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status="permanent_failure",
                        failure_kind=DeliveryFailureKind.RENDERER_FAILURE,
                        receipt=None,
                        error=exc.error,
                        duration_ms=elapsed,
                    )
                except asyncio.CancelledError:
                    # Shutdown cancellation must propagate cleanly and not
                    # be swallowed as a permanent delivery failure.
                    raise
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
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_failed()
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status=status,
                        failure_kind=failure_kind,
                        receipt=None,
                        error=error_msg,
                        duration_ms=elapsed,
                    )
            finally:
                if self._capacity_controller is not None:
                    await self._capacity_controller.release_delivery()

        return list(
            await asyncio.gather(*[_deliver_one(r, p) for r, p in route_targets])
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
        return "transient_failure" if kind.is_retryable else "permanent_failure"

    async def deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
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

        **Phase 1 implements a background retry scheduler via RetryWorker (opt-in).**
        When RetryWorker is not enabled, retry is synchronous/receipt-level only:
        this method records the failure receipt with ``next_retry_at`` populated.
        A future scheduler or manual replay re-invokes this method with the
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
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=f"Adapter {adapter_id!r} is not registered in the runtime "
                f"— the adapter may have failed to build or was not configured. "
                f"Check build logs for {adapter_id!r}",
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                retry_max_attempts=(
                    plan.retry_policy.max_attempts if plan.retry_policy else None
                ),
                retry_backoff_base=(
                    plan.retry_policy.backoff_base if plan.retry_policy else None
                ),
                retry_max_delay=(
                    plan.retry_policy.max_delay_seconds if plan.retry_policy else None
                ),
                retry_jitter=(plan.retry_policy.jitter if plan.retry_policy else None),
            )
            await self._config.storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                f"Adapter {adapter_id!r} is not registered — "
                f"check if the adapter was configured and built successfully",
                failure_kind=DeliveryFailureKind.ADAPTER_MISSING,
            ) from None

        # Check delivery plan deadline.
        if plan.deadline is not None and now > plan.deadline:
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error="Delivery deadline exceeded",
                failure_kind=DeliveryFailureKind.DEADLINE_EXCEEDED.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                retry_max_attempts=(
                    plan.retry_policy.max_attempts if plan.retry_policy else None
                ),
                retry_backoff_base=(
                    plan.retry_policy.backoff_base if plan.retry_policy else None
                ),
                retry_max_delay=(
                    plan.retry_policy.max_delay_seconds if plan.retry_policy else None
                ),
                retry_jitter=(plan.retry_policy.jitter if plan.retry_policy else None),
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
        # Enrich relations with target-adapter native refs so that the
        # renderer (and downstream adapter) receive native IDs for
        # structured replies / reactions.  This enrichment is per-target
        # and does not mutate the stored original event.
        render_event = await self._enrich_relations_for_target(event, adapter_id or "")
        target_platform = getattr(adapter, "platform", None)
        if isinstance(target_platform, str):
            platform_param: str | None = target_platform
        else:
            platform_param = None
        try:
            rendering_result = await self._rendering_pipeline.render(
                render_event,
                adapter_id or "",
                target.channel,
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
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=rendering_error,
                failure_kind=DeliveryFailureKind.RENDERER_FAILURE.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                retry_max_attempts=(
                    plan.retry_policy.max_attempts if plan.retry_policy else None
                ),
                retry_backoff_base=(
                    plan.retry_policy.backoff_base if plan.retry_policy else None
                ),
                retry_max_delay=(
                    plan.retry_policy.max_delay_seconds if plan.retry_policy else None
                ),
                retry_jitter=(plan.retry_policy.jitter if plan.retry_policy else None),
            )
            await self._config.storage.append_receipt(receipt)
            raise _RendererDeliveryError(adapter_id or "", rendering_error) from None

        # Guard: adapter must expose a callable deliver() method.
        deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
        if deliver_fn is None or not callable(deliver_fn):
            no_deliver_error = "Adapter has no deliver() method"
            self._log.warning(
                "Adapter %r has no deliver() method; event_id=%s",
                adapter_id,
                event.event_id,
            )
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=no_deliver_error,
                failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT.value,
                next_retry_at=None,
                created_at=now,
                attempt_number=attempt_number,
                parent_receipt_id=parent_receipt_id,
                source=source,
                replay_run_id=replay_run_id,
                retry_max_attempts=(
                    plan.retry_policy.max_attempts if plan.retry_policy else None
                ),
                retry_backoff_base=(
                    plan.retry_policy.backoff_base if plan.retry_policy else None
                ),
                retry_max_delay=(
                    plan.retry_policy.max_delay_seconds if plan.retry_policy else None
                ),
                retry_jitter=(plan.retry_policy.jitter if plan.retry_policy else None),
            )
            await self._config.storage.append_receipt(receipt)
            raise _AdapterDeliveryError(
                adapter_id or "",
                no_deliver_error,
                failure_kind=DeliveryFailureKind.ADAPTER_PERMANENT,
            ) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        adapter_result: AdapterDeliveryResult | None = None
        try:
            adapter_result = await deliver_fn(rendering_result)

            status: Literal["sent", "failed"] = "sent"
            error: str | None = None
            self._log.info(
                "Delivered: event_id=%s → adapter=%s plan=%s attempt=%d",
                event.event_id,
                adapter_id,
                plan.plan_id,
                attempt_number,
            )
        except asyncio.CancelledError:
            # CancelledError must propagate directly — never caught and
            # classified as a delivery failure.
            raise
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
        _receipt_failure_kind: str | None = None
        if status == "failed" and delivery_exc is not None:
            _receipt_failure_kind = RetryExecutor.classify_failure(
                delivery_exc,
                adapter_registered=True,
            ).value

        # Compute next_retry_at for retryable transient failures.
        # Only set when the plan declares an explicit retry_policy.
        _next_retry_at: datetime | None = None
        if (
            status == "failed"
            and _receipt_failure_kind == DeliveryFailureKind.ADAPTER_TRANSIENT.value
            and plan.retry_policy is not None
        ):
            executor = RetryExecutor(plan.retry_policy)
            if not executor.is_exhausted(attempt_number):
                backoff = executor.compute_backoff(attempt_number)
                _next_retry_at = now + backoff

        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id or "",
            target_channel=target.channel,
            route_id=route.id,
            status=status,
            error=error,
            failure_kind=_receipt_failure_kind,
            adapter_message_id=None,
            next_retry_at=_next_retry_at,
            created_at=now,
            attempt_number=attempt_number,
            parent_receipt_id=parent_receipt_id,
            source=source,
            replay_run_id=replay_run_id,
            retry_max_attempts=(
                plan.retry_policy.max_attempts if plan.retry_policy else None
            ),
            retry_backoff_base=(
                plan.retry_policy.backoff_base if plan.retry_policy else None
            ),
            retry_max_delay=(
                plan.retry_policy.max_delay_seconds if plan.retry_policy else None
            ),
            retry_jitter=(plan.retry_policy.jitter if plan.retry_policy else None),
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
                source=source,
                replay_run_id=replay_run_id,
                target_channel=target.channel,
            )
            await self._config.storage.append_receipt(dead_receipt)

        # Store native ref mapping (outbound direction) ONLY on success.
        # Use adapter-provided native IDs; never fabricate synthetic IDs.
        if (
            status == "sent"
            and adapter_result is not None
            and adapter_result.native_message_id is not None
        ):
            # Extract metadata from adapter result, converting
            # MappingProxyType to a plain dict for storage.
            outbound_meta: dict[str, object] = (
                dict(adapter_result.metadata) if adapter_result.metadata else {}
            )
            native_ref = NativeMessageRef(
                id=f"nref-{uuid.uuid4()}",
                event_id=event.event_id,
                adapter=adapter_id or "",
                native_channel_id=adapter_result.native_channel_id,
                native_message_id=adapter_result.native_message_id,
                native_thread_id=adapter_result.native_thread_id,
                native_relation_id=adapter_result.native_relation_id,
                direction="outbound",
                metadata=outbound_meta,
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

        return list(await asyncio.gather(*[_safe_deliver(r, p) for r, p in deliveries]))

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
