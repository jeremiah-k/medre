"""Pipeline runner that orchestrates the full event lifecycle.

This module provides the central orchestration engine that wires together
the framework's subsystems into a coherent processing pipeline. Per-target
delivery sequencing is delegated to
:class:`~medre.core.engine.pipeline.delivery_coordinator.DeliveryCoordinator`;
rendering/adapter execution is delegated to
:class:`~medre.core.engine.pipeline.target_delivery.TargetDeliveryService`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Literal,
    TypedDict,
    cast,
)

import msgspec

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContract,
    OutboundNativeRefRecord,
)
from medre.core.engine.phases import PipelinePhase
from medre.core.engine.pipeline.delivery_coordinator import (
    DeliveryCoordinator,
    InflightDelivery,
    _bounded_ordered_map,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.delivery_state import (
    is_accepted_outcome_status as _is_accepted_outcome_status,
)
from medre.core.engine.pipeline.outbox_manager import (
    OUTBOX_CREATION_FAILED_REASON,
    OutboxManager,
)
from medre.core.engine.pipeline.target_delivery import TargetDeliveryService
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.kinds import EventKind
from medre.core.ingress import (
    AdmissionResult,
    DurableIngressDeferredError,
    IngressProvenance,
)
from medre.core.observability.correlation import correlation_scope
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.capabilities import (
    resolve_adapter_capabilities,
)
from medre.core.planning.conversation_graph import (
    ConversationGraphAuthority,
    ConversationProjectionService,
    ConversationRebuildSummary,
    ConversationRepairResult,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_enricher import RelationEnricher, SenderProjectionFn
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting

if TYPE_CHECKING:
    from medre.core.supervision.capacity import CapacityController

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
    project_sender_metadata_fn:
        Optional callback that projects a target :class:`CanonicalEvent`
        into a JSON-safe dict of generic sender fields
        (``source_sender_label``, ``source_sender_short_label``,
        ``source_sender_id``, ``source_sender_handle``) used by relation
        enrichment to populate ``original_sender_displayname`` /
        ``original_sender``.  Wired by the runtime builder with the
        adapter-local attribution dispatch; core never imports adapter
        projection helpers.  When ``None``, enrichment falls back to the
        generic :attr:`CanonicalEvent.source_transport_id` field for
        ``original_sender`` and leaves ``original_sender_displayname``
        unset.
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
    project_sender_metadata_fn: SenderProjectionFn | None = None


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


class PhaseSnapshot(TypedDict):
    """Stable diagnostic snapshot of pipeline phase instrumentation."""

    current_phase: str | None
    counts: dict[str, int]


class PipelineRunner:
    """Orchestrates the full event pipeline:

    ingress → store → route → plan → deliver → receipt.

    The runner is started and stopped via :meth:`start` and :meth:`stop`.
    Runtime adapters publish through :class:`MedreApp`'s durable admission
    callback; routing and delivery are resumed by the durable ingress worker.
    :meth:`handle_ingress` remains the explicit inline execution path for
    focused tests that intentionally bypass runtime durability.

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
    >>> # RuntimeBuilder/MedreApp wires durable adapter ingress in production.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._log: logging.Logger = config.logger or _logger
        self._diagnostician: Diagnostician = config.diagnostician or Diagnostician()
        self._rendering_pipeline: RenderingPipeline = (
            config.rendering_pipeline or _default_rendering_pipeline()
        )
        self._relation_enricher = RelationEnricher(
            storage=config.storage,
            logger=self._log,
        )
        self._project_sender_metadata_fn: SenderProjectionFn | None = (
            config.project_sender_metadata_fn
        )
        self._conversation_authority = ConversationGraphAuthority(
            storage=config.storage,
            logger=self._log,
        )
        self._conversation_projection = ConversationProjectionService(
            storage=config.storage,
            logger=self._log,
        )
        self._lifecycle = DeliveryLifecycleService(logger=self._log)
        self._outbox_manager = OutboxManager(
            storage=config.storage,
            lifecycle=self._lifecycle,
        )
        self._target_delivery = TargetDeliveryService(
            adapters=config.adapters,
            rendering_pipeline=self._rendering_pipeline,
            storage=config.storage,
            diagnostician=self._diagnostician,
            lifecycle=self._lifecycle,
            logger=self._log,
            native_ref_persisted_fn=self._repair_conversation_after_native_ref,
        )
        self._middleware: _PipelineLoggingMiddleware | None = None
        self._route_stats: RouteStats | None = config.route_stats
        self._runtime_accounting: RuntimeAccounting | None = config.runtime_accounting
        self._capacity_controller: CapacityController | None = None
        self._delivery_rejection_count: int = 0
        self._inflight_deliveries: dict[str, InflightDelivery] = {}
        self._delivery_coordinator = DeliveryCoordinator(
            storage=config.storage,
            adapters=config.adapters,
            lifecycle=self._lifecycle,
            outbox_manager=self._outbox_manager,
            diagnostician=self._diagnostician,
            persist_suppression_receipt=self._coordinator_persist_suppression_receipt,
            deliver_target=self._coordinator_deliver_to_target,
            inflight_deliveries=self._inflight_deliveries,
            record_capacity_rejection=self._record_delivery_capacity_rejection,
            logger=self._log,
            route_stats=self._route_stats,
            runtime_accounting=self._runtime_accounting,
        )
        self._conversation_projection_repair_failed: bool = False
        self._running: bool = False

        # -- Phase instrumentation ------------------------------------------
        self._current_phase: PipelinePhase | None = None
        self._phase_counts: dict[PipelinePhase, int] = {
            phase: 0 for phase in PipelinePhase
        }

    # -- Conversation projection --------------------------------------------

    async def rebuild_conversation_projection(self) -> ConversationRebuildSummary:
        """Rebuild derived conversation membership from immutable evidence.

        Called during runtime startup after storage initialization.  The
        operation is idempotent and repairs any partial projection left by a
        prior crash without rewriting canonical events or relation rows.
        """
        return await self._conversation_projection.rebuild_all()

    async def mark_conversation_projection_clean(self) -> None:
        """Persist the clean marker after an orderly runtime shutdown."""
        await self._conversation_projection.mark_clean()

    # -- Lifecycle ----------------------------------------------------------

    def phase_snapshot(self) -> PhaseSnapshot:
        """Return a stable diagnostic snapshot of phase instrumentation.

        Returns a dict with:
        - ``current_phase``: the phase currently being executed, or ``None``.
        - ``counts``: per-phase invocation counts keyed by phase string value.

        The snapshot is intended for diagnostics and tests — it does not
        drive pipeline behavior.
        """
        return {
            "current_phase": self._current_phase.value if self._current_phase else None,
            "counts": {
                phase.value: self._phase_counts[phase] for phase in PipelinePhase
            },
        }

    @property
    def running(self) -> bool:
        """Whether the pipeline has been started and not yet stopped."""
        return self._running

    @property
    def delivery_lifecycle(self) -> DeliveryLifecycleService:
        """Return the shared delivery lifecycle authority."""
        return self._lifecycle

    @property
    def conversation_projection_repair_failed(self) -> bool:
        """Whether any conversation projection repair failed this run."""
        return self._conversation_projection_repair_failed

    def _record_conversation_projection_repair_failure(self) -> None:
        """Latch projection repair failure until the next runner generation."""
        self._conversation_projection_repair_failed = True

    async def _repair_conversation_after_event_available(
        self,
        event_id: str,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
    ) -> ConversationRepairResult:
        """Repair one event and latch any failure before it can be suppressed."""
        try:
            return await self._conversation_projection.repair_after_event_available(
                event_id, get_fn=get_fn
            )
        except Exception:
            self._record_conversation_projection_repair_failure()
            raise

    async def start(self) -> None:
        """Register pipeline middleware with the event bus.

        Call this before runtime delivery workers or direct pipeline ingress run.

        On startup the runner populates the rendering pipeline's platform
        registry from the configured adapters so that renderer selection
        can use platform identity rather than adapter-name heuristics.

        Idempotent: calling ``start()`` when already running returns
        immediately without re-registering middleware.
        """
        if self._running:
            self._log.debug(
                "PipelineRunner.start() called while already running; skipping"
            )
            return

        self._conversation_projection_repair_failed = False
        middleware_registered = False
        try:
            self._middleware = _PipelineLoggingMiddleware()
            await self._config.event_bus.add_middleware(
                self._middleware, priority=100
            )
            middleware_registered = True
            self._populate_renderer_platforms()
        except BaseException:
            if middleware_registered and self._middleware is not None:
                try:
                    self._config.event_bus.remove_middleware(self._middleware)
                except Exception:
                    self._log.debug(
                        "Failed to rollback middleware after startup error",
                        exc_info=True,
                    )
            self._middleware = None
            self._running = False
            raise

        self._running = True
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
        """Wire a :class:`~medre.core.supervision.capacity.CapacityController`.

        When set, :class:`DeliveryCoordinator` acquires one slot for each
        accepted target delivery and owns its release across outbox creation,
        execution, finalization failure, and cancellation.
        """
        self._capacity_controller = cc
        self._delivery_coordinator.set_capacity_controller(cc)

    def _record_delivery_capacity_rejection(self) -> None:
        """Increment the runner-owned delivery rejection counter."""
        self._delivery_rejection_count += 1

    async def stop(self) -> None:
        """Remove pipeline middleware from the event bus.

        Safe to call even if :meth:`start` was never called.
        """
        if self._middleware is not None:
            self._config.event_bus.remove_middleware(self._middleware)
            self._middleware = None
        self._running = False
        self._log.info("PipelineRunner stopped")

    def drain_abandoned_deliveries(self) -> list[InflightDelivery]:
        """Return and clear all tracked in-flight deliveries.

        Called by :class:`~medre.runtime.app.MedreApp.stop()` after drain
        timeout expires to produce structured abandonment evidence.  After
        this call the internal registry is empty — callers own the returned
        list and should persist receipts before releasing the data.

        Returns
        -------
        list[InflightDelivery]
            In-flight delivery identity records that were abandoned due to
            drain timeout.  May be empty if all work completed in time.
        """
        abandoned = list(self._inflight_deliveries.values())
        self._inflight_deliveries.clear()
        return abandoned

    # -- Ingress -----------------------------------------------------------

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
        self._log.debug(
            "Ingress: event_id=%s kind=%s source=%s",
            event.event_id,
            event.event_kind,
            event.source_adapter,
        )

        # -- Per-ingress lookup caches (call-local, not instance-level) ---
        # Each handle_ingress call gets its own cache dicts so concurrent
        # calls on the same PipelineRunner instance cannot share, clear,
        # or contaminate each other's caches.
        _event_cache: dict[str, CanonicalEvent | None] = {}
        _refs_cache: dict[str, list[NativeMessageRef]] = {}
        # In-flight dedup: prevents duplicate concurrent storage calls when
        # fan-out delivery resolves the same event_id from multiple targets.
        _event_inflight: dict[str, asyncio.Task[CanonicalEvent | None]] = {}
        _refs_inflight: dict[str, asyncio.Task[list[NativeMessageRef]]] = {}

        async def _cached_get(event_id: str) -> CanonicalEvent | None:
            """Memoized storage.get for this ingress pass."""
            if event_id in _event_cache:
                return _event_cache[event_id]
            # Reuse an in-flight storage task if one is already running.
            if event_id in _event_inflight:
                return await asyncio.shield(_event_inflight[event_id])
            get_fn = getattr(self._config.storage, "get", None)
            if not callable(get_fn):
                return None

            async def _fetch_event() -> CanonicalEvent | None:
                return await cast(
                    Callable[[str], Awaitable[CanonicalEvent | None]], get_fn
                )(event_id)

            task: asyncio.Task[CanonicalEvent | None] = asyncio.create_task(
                _fetch_event()
            )
            _event_inflight[event_id] = task
            try:
                result = await task
                _event_cache[event_id] = result
                return result
            finally:
                _event_inflight.pop(event_id, None)

        async def _cached_list_native_refs(
            event_id: str,
        ) -> list[NativeMessageRef]:
            """Memoized storage.list_native_refs_for_event for this ingress pass."""
            if event_id in _refs_cache:
                return _refs_cache[event_id]
            # Reuse an in-flight storage task if one is already running.
            if event_id in _refs_inflight:
                return await asyncio.shield(_refs_inflight[event_id])
            list_fn = getattr(self._config.storage, "list_native_refs_for_event", None)
            if not callable(list_fn):
                return []

            async def _fetch_refs() -> list[NativeMessageRef]:
                return await cast(
                    Callable[[str], Awaitable[list[NativeMessageRef]]], list_fn
                )(event_id)

            rtask: asyncio.Task[list[NativeMessageRef]] = asyncio.create_task(
                _fetch_refs()
            )
            _refs_inflight[event_id] = rtask
            try:
                result = await rtask
                _refs_cache[event_id] = result
                return result
            finally:
                _refs_inflight.pop(event_id, None)

        # ── Phase: INGRESS ──────────────────────────────────────────────
        self._current_phase = PipelinePhase.INGRESS
        self._phase_counts[PipelinePhase.INGRESS] += 1

        # Stage 1 – validate
        self._validate_event(event)

        # ── Phase: DEDUP ────────────────────────────────────────────────
        self._current_phase = PipelinePhase.DEDUP
        self._phase_counts[PipelinePhase.DEDUP] += 1

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
                # A prior process may have committed the immutable event/native
                # facts but crashed during derived conversation repair.  Re-run
                # the idempotent projection repair before suppressing this
                # duplicate so replay can self-heal without a restart.
                try:
                    await self._repair_conversation_after_event_available(
                        existing_event_id
                    )
                except Exception:
                    self._log.exception(
                        "Failed to repair conversation projection for duplicate "
                        "native ref: event_id=%s",
                        existing_event_id,
                    )
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
                # NOTE(duplicate_suppressed): No DeliveryReceipt is persisted
                # here because this check runs at Stage 1.5 — *before* the
                # inbound event is stored (Stage 3).  There is no persisted
                # event_id to link a receipt to.  DUPLICATE_SUPPRESSED was
                # removed from the DeliveryFailureKind enum because it was
                # never emitted.  Evidence of this suppression is recorded
                # via RuntimeAccounting counters only.
                return []

        # Accounting: inbound event accepted past validation + dedup.
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_inbound_accepted()

        # ── Phase: RESOLVE_RELATIONS ────────────────────────────────────
        self._current_phase = PipelinePhase.RESOLVE_RELATIONS
        self._phase_counts[PipelinePhase.RESOLVE_RELATIONS] += 1

        # Stage 2 – resolve relations (pipeline-owned, not adapter/codec).
        event = await self._resolve_relations(event)

        # Stage 2.5 – assign conversation identity (root_event_id,
        # conversation_id) based on resolved relation targets.
        event = await self._assign_conversation_identity(event, get_fn=_cached_get)

        # ── Phase: STORE ────────────────────────────────────────────────
        self._current_phase = PipelinePhase.STORE
        self._phase_counts[PipelinePhase.STORE] += 1

        # Stage 3 – store
        await self.store_event(event)
        # Seed the call-local event cache with the exact immutable event shape
        # written above for later pipeline stages.  Projection repair below
        # deliberately bypasses pre-store lookups for every other event ID.
        _event_cache[event.event_id] = event

        # Stage 4 – persist inbound native ref
        await self._persist_inbound_native_ref(event)

        # Stage 4.25 – re-resolve relation targets against the now-current
        # native-ref map, then reconcile the rebuildable conversation
        # projection.  Neither operation rewrites the canonical row: the
        # refreshed relation target and conversation identity exist only on the
        # in-memory copy consumed by routing/rendering.  This distinction lets
        # a late native target converge both grouping and reply/reaction
        # rendering while preserving immutable ingress evidence.
        event = await self._resolve_relations(event)
        # Refresh the current-event entry after relation re-resolution.  Positive
        # ancestor hits are safe to reuse because canonical events are immutable,
        # but a cached miss may have become stale if another ingress stored that
        # parent concurrently between Stage 2.5 and this post-store repair.
        _event_cache[event.event_id] = event

        async def _projection_get(candidate_id: str) -> CanonicalEvent | None:
            cached = _event_cache.get(candidate_id)
            if cached is not None:
                return cached
            result = await self._config.storage.get(candidate_id)
            _event_cache[candidate_id] = result
            return result

        await self._repair_conversation_after_event_available(
            event.event_id, get_fn=_projection_get
        )
        event = await self._conversation_projection.project_event(event)

        # Stage 4.5 – suppress reaction-to-reaction
        if await self._is_reaction_to_reaction(event, get_fn=_cached_get):
            self._log.info(
                "Reaction-to-reaction suppressed: event_id=%s targets another reaction",
                event.event_id,
            )
            return []

        # Stages 5-6 – route, plan, deliver
        # ── Phase: ROUTE ────────────────────────────────────────────────
        self._current_phase = PipelinePhase.ROUTE
        self._phase_counts[PipelinePhase.ROUTE] += 1

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
        # ── Phase: DELIVER ──────────────────────────────────────────────
        self._current_phase = PipelinePhase.DELIVER
        self._phase_counts[PipelinePhase.DELIVER] += 1

        outcomes = await self.deliver_to_targets(
            event,
            deliveries,
            cached_get_fn=_cached_get,
            cached_list_fn=_cached_list_native_refs,
        )

        accepted = sum(1 for o in outcomes if _is_accepted_outcome_status(o.status))
        skipped = sum(1 for o in outcomes if o.status == "skipped")
        failed = sum(
            1
            for o in outcomes
            if o.status in ("transient_failure", "permanent_failure")
        )
        self._log.info(
            "Pipeline complete: event_id=%s targets=%d accepted=%d skipped=%d failed=%d",
            event.event_id,
            len(deliveries),
            accepted,
            skipped,
            failed,
        )

        return outcomes

    async def admit_ingress(
        self, event: CanonicalEvent, provenance: IngressProvenance
    ) -> AdmissionResult:
        """Durably admit one inbound event without routing it inline.

        Relation and conversation identity are resolved before the atomic
        storage boundary.  Storage then commits the canonical event, inbound
        native reference, and durable work marker in one transaction.
        """
        self._validate_event(event)
        with correlation_scope(
            trace_id=event.trace_id,
            event_id=event.event_id,
            source="ingress",
        ):
            event = await self._resolve_relations(event)
            event = await self._assign_conversation_identity(
                event, get_fn=self._config.storage.get
            )
            with correlation_scope(conversation_id=event.conversation_id):
                inbound_ref = self._build_inbound_native_ref(event)
                suppress_routing = provenance == "history"
                result = await self._config.storage.admit_ingress(
                    event,
                    inbound_ref,
                    provenance,
                    suppress_routing=suppress_routing,
                )
                # Repair on both new and duplicate admission.  If a previous
                # process committed ingress facts but failed during projection
                # repair, a repeated native admission is the recovery path that
                # completes the derived state before the source checkpoint can
                # advance.  Best-effort: a transient repair outage must not
                # propagate into the durable adapter and stall source-checkpoint
                # progress; startup rebuild is the eventual recovery path.
                try:
                    await self._repair_conversation_after_event_available(
                        result.event_id
                    )
                except Exception:
                    self._log.exception(
                        "Failed to repair conversation projection after "
                        "admit_ingress: event_id=%s",
                        result.event_id,
                    )
                if self._runtime_accounting is not None:
                    if result.created:
                        self._runtime_accounting.record_inbound_accepted()
                    else:
                        self._runtime_accounting.record_loop_prevented()
                return result

    async def process_admitted_event(self, event_id: str) -> list[DeliveryOutcome]:
        """Route and deliver a previously admitted durable ingress event.

        This method deliberately performs no event/native-ref persistence.
        The durable ingress worker calls it after claiming the work marker.
        Delivery creates deterministic outbox identities before each external
        send, so replay after a worker crash remains idempotent at the MEDRE
        work-state boundary.
        """
        stored_event = await self._config.storage.get(event_id)
        if stored_event is None:
            raise RuntimeError(f"admitted ingress event is missing: {event_id}")

        async def _projection_get(candidate_id: str) -> CanonicalEvent | None:
            # Projection derives from immutable stored evidence.  Reuse the
            # already-loaded admitted event while fetching only its ancestors
            # or dependents from storage.
            if candidate_id == event_id:
                return stored_event
            return await self._config.storage.get(candidate_id)

        # Stored relations are immutable ingress evidence.  Refresh unresolved
        # native targets on the in-memory delivery copy so work admitted before
        # its parent/native mapping existed can use the now-known target without
        # mutating history.
        event = await self._resolve_relations(stored_event)
        await self._repair_conversation_after_event_available(
            event_id, get_fn=_projection_get
        )
        event = await self._conversation_projection.project_event(event)
        with correlation_scope(
            trace_id=event.trace_id,
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            source="live",
        ):
            return await self._process_admitted_event_scoped(event)

    async def _process_admitted_event_scoped(
        self, event: CanonicalEvent
    ) -> list[DeliveryOutcome]:
        if await self._is_reaction_to_reaction(event):
            self._log.info(
                "Reaction-to-reaction suppressed from durable ingress: event_id=%s",
                event.event_id,
            )
            return []

        event, deliveries = await self.route_event(event)
        if not deliveries:
            self._log.info("No routes matched for event_id=%s", event.event_id)
            return []

        outcomes = await self.deliver_to_targets(event, deliveries)
        deferred_reasons: list[str] = []
        for outcome in outcomes:
            if outcome.failure_kind in {
                DeliveryFailureKind.CAPACITY_REJECTION,
                DeliveryFailureKind.SHUTDOWN_REJECTION,
            }:
                deferred_reasons.append(outcome.failure_kind.value)
                continue
            if (
                outcome.failure_kind is DeliveryFailureKind.OUTBOX_NOT_OWNED
                and outcome.failure_kind_detail == OUTBOX_CREATION_FAILED_REASON
            ):
                deferred_reasons.append(OUTBOX_CREATION_FAILED_REASON)
        if deferred_reasons:
            raise DurableIngressDeferredError(
                event.event_id, tuple(sorted(set(deferred_reasons)))
            )
        return outcomes

    async def render_replay_event(self, event: CanonicalEvent) -> list[RenderingResult]:
        """Re-render *event* using persisted live-delivery context.

        RE_RENDER deliberately does not re-route or re-plan.  Instead it
        reconstructs one deterministic rendering context per historical
        delivery identity from append-only receipt rendering evidence.
        Older evidence that predates a context field falls back only for that
        field; no current route defaults are substituted.
        """
        receipts = await self._config.storage.list_receipts_for_event(event.event_id)
        contexts: dict[tuple[str, str, str | None, str], dict[str, object]] = {}
        context_precedence: dict[tuple[str, str, str | None, str], int] = {}
        source_precedence = {"live": 3, "retry": 2, "replay": 1}
        for receipt in receipts:
            raw = receipt.rendering_evidence
            if not raw:
                continue
            try:
                evidence = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence, dict):
                continue
            # The append-only receipt is authoritative for target identity.
            # Rendering evidence supplies historical presentation inputs only;
            # older evidence may omit or contain stale target metadata. Prefer
            # the original live context over later retry/replay evidence.
            key = (
                receipt.delivery_plan_id,
                receipt.target_adapter,
                receipt.target_channel,
                receipt.route_id,
            )
            precedence = source_precedence.get(receipt.source, 0)
            if key not in contexts or precedence > context_precedence[key]:
                contexts[key] = evidence
                context_precedence[key] = precedence

        if not contexts:
            raise ValueError(
                f"No persisted rendering context for event_id={event.event_id}"
            )

        rendered: list[RenderingResult] = []
        for key in sorted(contexts, key=lambda item: tuple(x or "" for x in item)):
            evidence = contexts[key]
            _plan_id, adapter, channel, _route_id = key
            enriched = await self._enrich_relations_for_target(event, adapter, channel)
            strategy_raw = evidence.get("delivery_strategy")
            strategy: Literal["direct", "fallback_text"] = (
                "fallback_text" if strategy_raw == "fallback_text" else "direct"
            )
            capability_raw = evidence.get("capability_level")
            capability: Literal["native", "fallback", "unsupported"] = (
                capability_raw
                if isinstance(capability_raw, str)
                and capability_raw in {"native", "fallback", "unsupported"}
                else "native"
            )
            platform_raw = evidence.get("target_platform")
            platform = platform_raw if isinstance(platform_raw, str) else None
            max_chars = evidence.get("max_text_chars")
            max_bytes = evidence.get("max_text_bytes")
            origin_raw = evidence.get("source_origin_label")
            origin = origin_raw if isinstance(origin_raw, str) else None
            rendered.append(
                await self._rendering_pipeline.render(
                    enriched,
                    adapter,
                    channel,
                    target_platform=platform,
                    max_text_chars=max_chars if type(max_chars) is int else None,
                    max_text_bytes=max_bytes if type(max_bytes) is int else None,
                    delivery_strategy=strategy,
                    capability_level=capability,
                    source_origin_label=origin,
                )
            )
        return rendered

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

    async def _assign_conversation_identity(
        self,
        event: CanonicalEvent,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
    ) -> CanonicalEvent:
        """Assign root_event_id and conversation_id based on relation graph.

        Delegates to
        :class:`~medre.core.planning.conversation_graph.ConversationGraphAuthority`
        to walk the relation ancestry and determine the root event and
        conversation identity.  Uses the per-ingress cache via *get_fn*
        so ancestor lookups are reused across subsequent enrichment calls.
        """
        return await self._conversation_authority.resolve_conversation_identity(
            event,
            cached_get_fn=get_fn,
        )

    async def _enrich_relations_for_target(
        self,
        event: CanonicalEvent,
        target_adapter: str,
        target_channel: str | None = None,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        list_fn: Callable[[str], Awaitable[list[NativeMessageRef]]] | None = None,
    ) -> CanonicalEvent:
        """Enrich relations with target-adapter native refs for rendering.

        Delegates to :class:`~medre.core.planning.relation_enricher.RelationEnricher`.
        See that class for enrichment semantics.

        Passes per-ingress cached ``get`` and ``list_native_refs_for_event``
        callables so that lookups performed during reaction-to-reaction
        checks are reused here, and lookups across multiple target
        enrichments share results.  Forwards the runtime-wired
        :attr:`_project_sender_metadata_fn` so sender labels are sourced
        from generic projected fields rather than native identity keys.
        """
        return await self._relation_enricher.enrich_for_target(
            event,
            target_adapter=target_adapter,
            target_channel=target_channel,
            cached_get_fn=get_fn,
            cached_list_fn=list_fn,
            project_sender_fn=self._project_sender_metadata_fn,
        )

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

        inbound_ref = self._build_inbound_native_ref(event)
        if inbound_ref is not None:
            await self._config.storage.store_native_ref(inbound_ref)

    @staticmethod
    def _build_inbound_native_ref(event: CanonicalEvent) -> NativeMessageRef | None:
        """Build the persisted inbound native-ref record for *event*."""
        snr = event.source_native_ref
        if snr is None or not snr.native_message_id:
            return None
        return NativeMessageRef(
            id=f"nref-inbound-{uuid.uuid4()}",
            event_id=event.event_id,
            adapter=snr.adapter,
            native_channel_id=snr.native_channel_id,
            native_message_id=snr.native_message_id,
            native_thread_id=snr.native_thread_id,
            native_relation_id=None,
            direction="inbound",
            metadata=_native_metadata_for_ref(event),
            created_at=datetime.now(tz=timezone.utc),
        )

    # -- Per-ingress lookup cache helpers ------------------------------------

    async def _is_reaction_to_reaction(
        self,
        event: CanonicalEvent,
        *,
        get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
    ) -> bool:
        """Return ``True`` when *event* is a reaction whose target is itself a reaction.

        Checks each relation with ``relation_type == "reaction"`` for a
        ``target_event_id``.  If the target event exists in storage and is
        either a ``MESSAGE_REACTED`` event or carries a ``"reaction"``
        relation itself, the inbound event is considered a
        *reaction-to-reaction* and should be suppressed from routing.

        When *get_fn* is provided, uses it as a cached ``storage.get``
        callable so that lookups performed here are reused by subsequent
        relation enrichment for the same target_event_id.

        Failures to fetch the target event are logged and silently skipped
        so that storage errors never prevent delivery.
        """
        if event.event_kind != EventKind.MESSAGE_REACTED:
            return False
        if not event.relations:
            return False
        _get = get_fn or getattr(self._config.storage, "get", None)
        for rel in event.relations:
            if rel.relation_type != "reaction":
                continue
            target_id = rel.target_event_id
            if not target_id:
                continue
            try:
                if _get is not None:
                    target_event = await cast(
                        Callable[[str], Awaitable[CanonicalEvent | None]], _get
                    )(target_id)
                else:
                    target_event = None
            except Exception:
                self._log.debug(
                    "Failed to fetch target event for reaction-to-reaction check: %s",
                    target_id,
                    exc_info=True,
                )
                continue
            if target_event is None:
                continue
            # Target is itself a reaction event.
            if getattr(target_event, "event_kind", None) == EventKind.MESSAGE_REACTED:
                return True
            # Target has a reaction relation.
            target_rels = getattr(target_event, "relations", None)
            if target_rels:
                for target_rel in target_rels:
                    if getattr(target_rel, "relation_type", None) == "reaction":
                        return True
        return False

    async def _repair_conversation_after_native_ref(self, event_id: str) -> None:
        """Best-effort repair after an outbound native identity is persisted.

        The external delivery has already succeeded when this callback runs, so
        a derived-projection failure must never retroactively turn that send
        into a delivery failure.  Startup rebuild remains the durable recovery
        path.
        """
        try:
            await self._conversation_projection.repair_after_native_ref_available(event_id)
        except Exception:
            self._record_conversation_projection_repair_failure()
            self._log.exception(
                "Failed to repair conversation projection after native-ref "
                "persistence: event_id=%s",
                event_id,
            )

    async def _record_outbound_native_ref(
        self, record: OutboundNativeRefRecord
    ) -> None:
        """Atomically finalize a delayed queue-backed outbound send.

        Queue adapters call this after the external SDK returns a real native
        message ID.  Core validates exact outbox/attempt correlation and then
        commits the outbound native ref, supplemental ``sent`` receipt, and
        outbox terminal transition in one storage transaction.

        Callback failures are logged rather than raised into the adapter queue
        drain; stale callbacks commit no finalization evidence.
        """
        if not record.native_message_id:
            return

        try:
            await self._finalize_queued_delivery(
                record=record,
                now=datetime.now(tz=timezone.utc),
            )
            await self._repair_conversation_after_native_ref(record.event_id)
        except Exception:
            self._log.exception(
                "Failed to finalize delayed outbound delivery: "
                "event_id=%s adapter=%s native_message_id=%s",
                record.event_id,
                record.adapter,
                record.native_message_id,
            )

    async def _finalize_queued_delivery(
        self,
        record: OutboundNativeRefRecord,
        now: datetime,
    ) -> None:
        """Delegate queue-backed finalization to lifecycle authority."""
        await self._lifecycle.finalize_queued_delivery(
            self._config.storage,
            record=record,
            now=now,
        )

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

            for target_index, target in enumerate(targets):
                capabilities = self._get_adapter_capabilities(target)
                plan = self._config.fallback_resolver.resolve_fallback(
                    event,
                    target,
                    capabilities,
                    route_id=route.id,
                    target_index=target_index,
                )
                plan.route_id = route.id
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
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        cached_list_fn: (
            Callable[[str], Awaitable[list[NativeMessageRef]]] | None
        ) = None,
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
        cached_get_fn:
            Optional memoized ``storage.get`` callable scoped to a
            single ingress pass.
        cached_list_fn:
            Optional memoized ``storage.list_native_refs_for_event``
            callable scoped to a single ingress pass.

        Returns
        -------
        list[DeliveryOutcome]
            One :class:`DeliveryOutcome` per target, preserving the
            order of *route_targets*.
        """
        # Per-target capacity acquire/release happens inside _deliver_single_target().
        return await self._deliver_to_targets_fan_out(
            event,
            route_targets,
            source=source,
            replay_run_id=replay_run_id,
            cached_get_fn=cached_get_fn,
            cached_list_fn=cached_list_fn,
        )

    async def _persist_suppression_receipt(
        self,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        route_id: str,
        failure_kind: DeliveryFailureKind,
        error: str,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        """Build and persist a lightweight suppression/rejection receipt.

        Delegates to
        :class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.

        See :meth:`DeliveryLifecycleService.build_and_persist_suppression_receipt`
        for full documentation.
        """
        return await self._lifecycle.build_and_persist_suppression_receipt(
            self._config.storage,
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            failure_kind=failure_kind,
            error=error,
            source=source,
            replay_run_id=replay_run_id,
        )

    async def _coordinator_persist_suppression_receipt(
        self,
        *,
        event_id: str,
        delivery_plan_id: str,
        target_adapter: str,
        target_channel: str | None,
        route_id: str,
        failure_kind: DeliveryFailureKind,
        error: str,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> DeliveryReceipt:
        """Late-bind suppression persistence for delivery coordination."""
        return await self._persist_suppression_receipt(
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            failure_kind=failure_kind,
            error=error,
            source=source,
            replay_run_id=replay_run_id,
        )

    async def _coordinator_deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        cached_list_fn: (
            Callable[[str], Awaitable[list[NativeMessageRef]]] | None
        ) = None,
        outbox_id: str | None = None,
    ) -> DeliveryReceipt:
        """Late-bind target delivery so controlled test hooks remain effective."""
        return await self.deliver_to_target(
            event,
            route,
            plan,
            previous_receipt=previous_receipt,
            source=source,
            replay_run_id=replay_run_id,
            cached_get_fn=cached_get_fn,
            cached_list_fn=cached_list_fn,
            outbox_id=outbox_id,
        )

    async def _deliver_to_targets_fan_out(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Route, DeliveryPlan]],
        *,
        source: str = "live",
        replay_run_id: str | None = None,
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        cached_list_fn: (
            Callable[[str], Awaitable[list[NativeMessageRef]]] | None
        ) = None,
    ) -> list[DeliveryOutcome]:
        """Delegate bounded per-target coordination to ``DeliveryCoordinator``."""
        return await self._delivery_coordinator.deliver_many(
            event,
            route_targets,
            source=source,
            replay_run_id=replay_run_id,
            cached_get_fn=cached_get_fn,
            cached_list_fn=cached_list_fn,
        )

    async def deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
        cached_get_fn: Callable[[str], Awaitable[CanonicalEvent | None]] | None = None,
        cached_list_fn: (
            Callable[[str], Awaitable[list[NativeMessageRef]]] | None
        ) = None,
        outbox_id: str | None = None,
    ) -> DeliveryReceipt:
        """Deliver *event* to a single target adapter and record the receipt.

        Performs per-target relation enrichment (resolving target-event IDs
        to target-adapter native refs) before delegating to
        :class:`TargetDeliveryService` for rendering, adapter invocation,
        receipt creation, and native-ref persistence.

        The enriched ``render_event`` is passed to the service so that
        rendering and adapter delivery use target-specific native refs,
        while the original *event* identity is preserved for receipts.

        See :meth:`TargetDeliveryService.deliver_to_target` for full
        documentation of the delivery steps.
        """
        target = plan.target
        adapter_id = target.adapter or ""
        render_event = await self._enrich_relations_for_target(
            event,
            target_adapter=adapter_id,
            target_channel=target.channel,
            get_fn=cached_get_fn,
            list_fn=cached_list_fn,
        )
        return await self._target_delivery.deliver_to_target(
            event,
            route,
            plan,
            render_event=render_event,
            previous_receipt=previous_receipt,
            source=source,
            replay_run_id=replay_run_id,
            outbox_id=outbox_id,
        )

    # -- Internal helpers --------------------------------------------------

    def _get_adapter_capabilities(self, target: RouteTarget) -> AdapterCapabilities:
        """Retrieve the :class:`AdapterCapabilities` for a target adapter.

        Delegates to :func:`~medre.core.planning.capabilities.resolve_adapter_capabilities`
        with the configured adapter registry.  When the adapter is missing
        from the registry (yields ``None``), falls back to a default
        :class:`AdapterCapabilities` as a conservative internal default
        used only after adapter-missing checks — the pipeline has its own
        adapter-missing check at Phase 2.5.
        """
        caps = resolve_adapter_capabilities(self._config.adapters, target)
        if caps is None:
            return AdapterCapabilities()
        return caps
