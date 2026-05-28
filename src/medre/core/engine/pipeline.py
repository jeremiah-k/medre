"""Pipeline runner that orchestrates the full event lifecycle.

This module provides the central orchestration engine that wires together
the framework's subsystems into a coherent processing pipeline:

* :class:`PipelineConfig` – configuration dataclass wiring all dependencies.
* :class:`PipelineRunner` – async pipeline that processes events from
  ingress through storage, routing, planning, delivery, and receipt
  recording.

Pipeline stages
---------------
1. **Ingress** – validate required fields on inbound events.
2. **Dedup** – suppress duplicate native-message refs.
3. **Resolve Relations** – cross-adapter relation resolution.
4. **Store** – persist the event and inbound native ref.
5. **Route** – match against routes, create delivery plans.
6. **Deliver** – per-target render, send, receipt, outbox update.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    TypedDict,
    cast,
    get_args,
)

import msgspec

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContract,
    AdapterDeliveryResult,
    OutboundNativeRefRecord,
)
from medre.core.engine.phases import PipelinePhase
from medre.core.events.bus import EventBus
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.kinds import EventKind
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.capabilities import (
    capability_unsupported,
    resolve_adapter_capabilities,
)
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
    RetryExecutor,
    RetryPolicy,
)
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_enricher import RelationEnricher
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.policies.route_policy import BLOCKED_VALUE_CUTOFF, evaluate_route_policy
from medre.core.rendering.renderer import DeliveryStrategyMethod, RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting

if TYPE_CHECKING:
    from medre.core.supervision.capacity import CapacityController


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Outbox lease-renewal tuning constants
# ---------------------------------------------------------------------------

_OUTBOX_RENEWAL_INTERVAL_SECONDS: int = 30  # seconds between lease renewals
_OUTBOX_RENEWAL_DURATION_SECONDS: int = 60  # lease TTL (kept short; renewed)

# ---------------------------------------------------------------------------
# Delivery strategy method validation
# ---------------------------------------------------------------------------

#: Mapping from raw strategy strings to their :data:`DeliveryStrategyMethod`
#: typed values.  Used by :func:`_validate_strategy_method` to validate and
#: narrow ``DeliveryStrategy.method`` (typed ``str``) to the strict
#: ``DeliveryStrategyMethod`` literal accepted by
#: :meth:`RenderingPipeline.render`.
_VALID_DELIVERY_STRATEGIES: dict[str, DeliveryStrategyMethod] = {
    m: m for m in get_args(DeliveryStrategyMethod)
}


def _validate_strategy_method(method: str) -> DeliveryStrategyMethod:
    """Validate *method* against known delivery strategy literals.

    Returns the :data:`DeliveryStrategyMethod`-typed value on success so
    callers can pass it directly to :meth:`RenderingPipeline.render`.

    Raises :class:`ValueError` for unknown strategy strings.
    """
    try:
        return _VALID_DELIVERY_STRATEGIES[method]
    except KeyError:
        raise ValueError(f"Unknown delivery strategy method: {method!r}") from None


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


# ---------------------------------------------------------------------------
# In-flight delivery tracking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InflightDelivery:
    """Identity record for a delivery currently in-flight in the pipeline.

    Used by :meth:`PipelineRunner.drain_abandoned_deliveries` to produce
    structured shutdown evidence when drain timeout expires.

    Attributes
    ----------
    event_id:
        Canonical event ID being delivered.
    route_id:
        Route that matched this delivery.
    target_adapter:
        Target adapter for this delivery.
    target_channel:
        Channel on the target adapter, if applicable.
    delivery_plan_id:
        ID of the delivery plan governing this attempt.
    source:
        Origin of delivery: ``"live"``, ``"retry"``, or ``"replay"``.
    replay_run_id:
        When ``source="replay"``, the replay run identifier.
    acquired_at:
        Monotonic timestamp when the delivery slot was acquired.
    outbox_id:
        ID of the outbox item tracking this delivery, if created.
    """

    event_id: str
    route_id: str
    target_adapter: str
    target_channel: str | None
    delivery_plan_id: str
    source: str
    replay_run_id: str | None
    acquired_at: float
    outbox_id: str | None = None


class _AdapterDeliveryError(Exception):
    """Raised by ``deliver_to_target`` after persisting a failed receipt.

    Carries the adapter ID, error string, the original exception,
    an optional pre-classified ``failure_kind``, and the persisted
    ``receipt`` so that callers can produce a deterministic
    :class:`DeliveryOutcome` without re-inspecting the exception type
    and can correlate the outbox row with the actual receipt.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        original: Exception | None = None,
        *,
        failure_kind: DeliveryFailureKind | None = None,
        receipt: DeliveryReceipt | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.original = original
        self.failure_kind = failure_kind
        self.receipt = receipt
        super().__init__(error)


class _RendererDeliveryError(Exception):
    """Raised by ``deliver_to_target`` when rendering fails before delivery.

    Carries the adapter ID, error string, and optional persisted
    ``receipt`` so callers can produce a deterministic
    :class:`DeliveryOutcome` and correlate the outbox row.
    """

    def __init__(
        self,
        adapter_id: str,
        error: str,
        *,
        receipt: DeliveryReceipt | None = None,
        failure_kind: DeliveryFailureKind | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.receipt = receipt
        self.failure_kind = failure_kind
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


class PhaseSnapshot(TypedDict):
    """Stable diagnostic snapshot of pipeline phase instrumentation."""

    current_phase: str | None
    counts: dict[str, int]


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
        self._relation_enricher = RelationEnricher(
            storage=config.storage,
            logger=self._log,
        )
        self._middleware: _PipelineLoggingMiddleware | None = None
        self._route_stats: RouteStats | None = config.route_stats
        self._runtime_accounting: RuntimeAccounting | None = config.runtime_accounting
        self._capacity_controller: CapacityController | None = None
        self._delivery_rejection_count: int = 0
        self._inflight_deliveries: dict[str, InflightDelivery] = {}
        self._running: bool = False

        # -- Phase instrumentation ------------------------------------------
        self._current_phase: PipelinePhase | None = None
        self._phase_counts: dict[PipelinePhase, int] = {
            phase: 0 for phase in PipelinePhase
        }

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

    async def start(self) -> None:
        """Register pipeline middleware with the event bus.

        Call this before any adapter calls :attr:`ingress_handler`.

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

        middleware_registered = False
        try:
            self._middleware = _PipelineLoggingMiddleware()
            self._config.event_bus.add_middleware(self._middleware, priority=100)
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
        self._log.debug(
            "Ingress: event_id=%s kind=%s source=%s",
            event.event_id,
            event.event_kind,
            event.source_adapter,
        )

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

        # ── Phase: STORE ────────────────────────────────────────────────
        self._current_phase = PipelinePhase.STORE
        self._phase_counts[PipelinePhase.STORE] += 1

        # Stage 3 – store
        await self.store_event(event)

        # Stage 4 – persist inbound native ref
        await self._persist_inbound_native_ref(event)

        # Stage 4.5 – suppress reaction-to-reaction
        if await self._is_reaction_to_reaction(event):
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

        outcomes = await self.deliver_to_targets(event, deliveries)

        accepted = sum(1 for o in outcomes if o.status in {"success", "queued"})
        failed = len(outcomes) - accepted
        self._log.info(
            "Pipeline complete: event_id=%s targets=%d accepted=%d failed=%d",
            event.event_id,
            len(deliveries),
            accepted,
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
        target_channel: str | None = None,
    ) -> CanonicalEvent:
        """Enrich relations with target-adapter native refs for rendering.

        Delegates to :class:`~medre.core.planning.relation_enricher.RelationEnricher`.
        See that class for enrichment semantics.
        """
        return await self._relation_enricher.enrich_for_target(
            event,
            target_adapter=target_adapter,
            target_channel=target_channel,
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

    async def _is_reaction_to_reaction(self, event: CanonicalEvent) -> bool:
        """Return ``True`` when *event* is a reaction whose target is itself a reaction.

        Checks each relation with ``relation_type == "reaction"`` for a
        ``target_event_id``.  If the target event exists in storage and is
        either a ``MESSAGE_REACTED`` event or carries a ``"reaction"``
        relation itself, the inbound event is considered a
        *reaction-to-reaction* and should be suppressed from routing.

        Failures to fetch the target event are logged and silently skipped
        so that storage errors never prevent delivery.
        """
        if event.event_kind != EventKind.MESSAGE_REACTED:
            return False
        if not event.relations:
            return False
        get_fn = getattr(self._config.storage, "get", None)
        if not callable(get_fn):
            return False
        for rel in event.relations:
            if rel.relation_type != "reaction":
                continue
            target_id = rel.target_event_id
            if not target_id:
                continue
            try:
                target_event = await cast(Callable[[str], Awaitable[object]], get_fn)(
                    target_id
                )
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

    async def _record_outbound_native_ref(
        self, record: OutboundNativeRefRecord
    ) -> None:
        """Persist a delayed outbound :class:`NativeMessageRef`.

        Called by queue-based adapters after a queued send returns a real
        native message ID.  This is the callback wired into
        :class:`AdapterContext.record_outbound_native_ref`.

        The guard against empty ``native_message_id`` is a defensive check
        for manually-constructed records; note that
        :class:`OutboundNativeRefRecord` now rejects empty values at
        construction.  Catches and logs all exceptions with
        ``exc_info=True`` so that callback failures never crash the
        adapter's queue drain.

        After storing the native ref, appends a supplemental delivery
        receipt with ``status="sent"`` and the real native message ID.
        This bridges the evidence gap for queue-based adapters: the
        initial receipt is ``status="queued"`` (from ``delivery_status``
        ``"enqueued"``), and this supplemental receipt records the
        transition to "sent" when the queue drain produces a real
        native ID.

        Parameters
        ----------
        record:
            The outbound native reference record from the adapter.
        """
        if not record.native_message_id:
            return

        try:
            now = datetime.now(tz=timezone.utc)
            outbound_ref = NativeMessageRef(
                id=f"nref-outbound-{uuid.uuid4()}",
                event_id=record.event_id,
                adapter=record.adapter,
                native_channel_id=record.native_channel_id,
                native_message_id=record.native_message_id,
                native_thread_id=record.native_thread_id,
                native_relation_id=record.native_relation_id,
                direction="outbound",
                metadata=dict(record.metadata),
                created_at=now,
            )
            await self._config.storage.store_native_ref(outbound_ref)

            # Append a supplemental "sent" receipt to close the
            # queued → sent evidence gap for queue-based adapters.
            # Look up the most recent "queued" receipt for this
            # event + adapter to inherit plan/route context.
            try:
                await self._append_queued_to_sent_receipt(record=record, now=now)
            except Exception:
                self._log.exception(
                    "Failed to append supplemental sent receipt: "
                    "event_id=%s adapter=%s",
                    record.event_id,
                    record.adapter,
                )
        except Exception:
            self._log.exception(
                "Failed to record delayed outbound native ref: "
                "event_id=%s adapter=%s native_message_id=%s",
                record.event_id,
                record.adapter,
                record.native_message_id,
            )

    async def _append_queued_to_sent_receipt(
        self,
        record: OutboundNativeRefRecord,
        now: datetime,
    ) -> None:
        """Append a supplemental ``status="sent"`` receipt for a queue-based
        delivery that transitioned from enqueued to sent.

        Finds the most recent ``status="queued"`` receipt for this
        event_id + adapter, inherits its plan/route context, and appends
        a new immutable receipt with ``status="sent"`` and the real
        ``adapter_message_id``.

        If no matching ``"queued"`` receipt is found (e.g. non-queued
        adapter or replay context), the method returns silently.

        Parameters
        ----------
        record:
            The outbound native reference record from the adapter.
        now:
            Timestamp for the new receipt.
        """
        # Look up existing receipts for this event to find the
        # queued receipt we want to supplement.
        try:
            existing = await self._config.storage.list_receipts_for_event(
                record.event_id
            )
        except Exception:
            return

        # Find the most recent "queued" receipt targeting this adapter.
        # Match by event_id (implicit via list_receipts_for_event),
        # adapter, and — when available — target_channel.
        candidates: list[DeliveryReceipt] = [
            r
            for r in existing
            if r.status == "queued" and r.target_adapter == record.adapter
        ]

        if not candidates:
            return

        if record.native_channel_id is not None:
            # Narrow to exact channel match.
            channel_matches = [
                r for r in candidates if r.target_channel == record.native_channel_id
            ]
            if not channel_matches:
                self._log.debug(
                    "No queued receipt matched channel %s for "
                    "event_id=%s adapter=%s; skipping supplemental receipt",
                    record.native_channel_id,
                    record.event_id,
                    record.adapter,
                )
                return
            # Most recent (last in list) wins — handles retries.
            queued_receipt = channel_matches[-1]
        else:
            # No channel on record — disambiguate by count.
            if len(candidates) == 1:
                queued_receipt = candidates[0]
            else:
                self._log.debug(
                    "Ambiguous queued receipt correlation: %d candidates "
                    "for event_id=%s adapter=%s with no channel; "
                    "skipping supplemental receipt",
                    len(candidates),
                    record.event_id,
                    record.adapter,
                )
                return

        supplemental = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=record.event_id,
            delivery_plan_id=queued_receipt.delivery_plan_id,
            target_adapter=record.adapter,
            target_channel=record.native_channel_id or queued_receipt.target_channel,
            route_id=queued_receipt.route_id,
            status="sent",
            error=None,
            failure_kind=None,
            adapter_message_id=record.native_message_id,
            next_retry_at=None,
            created_at=now,
            attempt_number=queued_receipt.attempt_number,
            parent_receipt_id=queued_receipt.receipt_id,
            source=queued_receipt.source,
            replay_run_id=getattr(queued_receipt, "replay_run_id", None),
            retry_max_attempts=getattr(queued_receipt, "retry_max_attempts", None),
            retry_backoff_base=getattr(queued_receipt, "retry_backoff_base", None),
            retry_max_delay=getattr(queued_receipt, "retry_max_delay", None),
            retry_jitter=getattr(queued_receipt, "retry_jitter", None),
        )
        await self._config.storage.append_receipt(supplemental)

        # Transition the matching outbox item from queued → sent.
        # The item may still be in_progress if the callback fires before
        # _deliver_one() marks the outbox row as queued.  Prefer queued
        # status over in_progress so that a fully-queued row is always
        # selected first.
        try:
            _obi = await self._config.storage.get_outbox_item_for_delivery(
                event_id=record.event_id,
                delivery_plan_id=queued_receipt.delivery_plan_id,
                target_adapter=record.adapter,
                target_channel=queued_receipt.target_channel,
                status="queued",
            )
            if _obi is None:
                _obi = await self._config.storage.get_outbox_item_for_delivery(
                    event_id=record.event_id,
                    delivery_plan_id=queued_receipt.delivery_plan_id,
                    target_adapter=record.adapter,
                    target_channel=queued_receipt.target_channel,
                    status="in_progress",
                )
            if _obi is not None:
                await self._config.storage.mark_outbox_sent(
                    _obi.outbox_id,
                    receipt_id=supplemental.receipt_id,
                    attempt_number=supplemental.attempt_number,
                )
        except Exception:
            self._log.exception(
                "Failed to transition outbox queued→sent: " "event_id=%s adapter=%s",
                record.event_id,
                record.adapter,
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

        Creates a ``status="suppressed"`` :class:`DeliveryReceipt` with
        ``attempt_number=1``, no ``next_retry_at``, and the given
        *failure_kind*.  The receipt is appended to storage so downstream
        reporting can inspect loop suppression, capacity rejection, and
        shutdown rejection events.

        Parameters
        ----------
        event_id:
            The canonical event ID (must already be persisted).
        delivery_plan_id:
            ID of the delivery plan.
        target_adapter:
            Name of the target adapter.
        target_channel:
            Channel on the target adapter, if applicable.
        route_id:
            ID of the route that triggered this delivery.
        failure_kind:
            The :class:`DeliveryFailureKind` for the suppression reason.
        error:
            Human-readable error/reason string.
        source:
            Origin of delivery: ``"live"``, ``"retry"``, or ``"replay"``.
        replay_run_id:
            When ``source="replay"``, the replay run identifier.

        Returns
        -------
        DeliveryReceipt
            The persisted suppression receipt.
        """
        now = datetime.now(tz=timezone.utc)
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=f"rcpt-{uuid.uuid4()}",
            event_id=event_id,
            delivery_plan_id=delivery_plan_id,
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            status="suppressed",
            error=error,
            failure_kind=failure_kind.value,
            next_retry_at=None,
            created_at=now,
            attempt_number=1,
            parent_receipt_id=None,
            source=source,
            replay_run_id=replay_run_id,
        )
        await self._config.storage.append_receipt(receipt)
        return receipt

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

            # ── Phase 1: Loop checks (no state mutation) ──────────────

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
                    loop_receipt = await self._persist_suppression_receipt(
                        event_id=event.event_id,
                        delivery_plan_id=route_plan.plan_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
                        error="loop_prevented: route already traversed in prior routing pass",
                        source=source,
                        replay_run_id=replay_run_id,
                    )
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status="skipped",
                        failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
                        receipt=loop_receipt,
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
                selfloop_receipt = await self._persist_suppression_receipt(
                    event_id=event.event_id,
                    delivery_plan_id=route_plan.plan_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
                    error="loop_prevented",
                    source=source,
                    replay_run_id=replay_run_id,
                )
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=route_plan.plan_id,
                    status="skipped",
                    failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
                    receipt=selfloop_receipt,
                    error="loop_prevented",
                    duration_ms=elapsed,
                )

            # ── Phase 2: Route-policy evaluation (no state mutation) ──

            # Route-policy evaluation: enforce allowlists attached
            # to the route.  Runs after structural loop/self-loop
            # checks but BEFORE capacity acquisition so that
            # policy-denied targets never consume capacity or
            # increment capacity_rejection counters.
            if route.policy is not None:
                decision = evaluate_route_policy(
                    route.policy,
                    event,
                    target,
                )
                if not decision.allowed:
                    # Sanitize blocked_value FIRST: cap at 256 chars to prevent
                    # large externally-sourced IDs from flooding logs/receipts.
                    _blocked_val = decision.blocked_value or ""
                    if len(_blocked_val) >= BLOCKED_VALUE_CUTOFF:
                        _blocked_val = _blocked_val[:BLOCKED_VALUE_CUTOFF] + "..."
                    self._log.info(
                        "policy_suppressed: route_id=%s event_id=%s "
                        "target_adapter=%s reason=%s "
                        "blocked_field=%s blocked_value=%r",
                        route.id,
                        event.event_id,
                        adapter_id,
                        decision.reason,
                        decision.blocked_field,
                        _blocked_val,
                    )
                    if self._route_stats is not None:
                        self._route_stats.record_policy_suppressed(route.id)
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_policy_suppressed()
                    elapsed = (time.monotonic() - t0) * 1000.0
                    # Build safe error text with reason, blocked field,
                    # capped blocked value, and the allowed summary.
                    policy_error = (
                        f"policy_suppressed: {decision.reason} "
                        f"({decision.blocked_field}={_blocked_val!r}); "
                        f"{decision.allowed_summary}"
                    )
                    policy_receipt = await self._persist_suppression_receipt(
                        event_id=event.event_id,
                        delivery_plan_id=route_plan.plan_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
                        error=policy_error,
                        source=source,
                        replay_run_id=replay_run_id,
                    )
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status="skipped",
                        failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
                        receipt=policy_receipt,
                        error=policy_error,
                        duration_ms=elapsed,
                    )

            # ── Phase 2.5: Capability check (no state mutation) ──────

            # Capability suppression: skip delivery when the target
            # adapter does not support the event kind or required
            # delivery features.  Runs after route-policy checks but
            # BEFORE capacity acquisition so that capability-unsupported
            # targets never consume capacity or increment counters.
            #
            # IMPORTANT: Only run the capability check for adapters
            # that are actually registered.  Unknown / missing adapters
            # must NOT be capability-suppressed — they need to fall
            # through to deliver_to_target() which produces the correct
            # ADAPTER_MISSING outcome with a meaningful error message.
            _suppression_reason: str | None = None
            if adapter_id and adapter_id in self._config.adapters:
                _caps = self._get_adapter_capabilities(target)
                _suppression_reason = capability_unsupported(event, _caps)
            if _suppression_reason is not None:
                self._log.info(
                    "capability_suppressed: route_id=%s event_id=%s "
                    "target_adapter=%s reason=%s",
                    route.id,
                    event.event_id,
                    adapter_id,
                    _suppression_reason,
                )
                if self._route_stats is not None:
                    self._route_stats.record_capability_suppressed(route.id)
                if self._runtime_accounting is not None:
                    self._runtime_accounting.record_capability_suppressed()
                elapsed = (time.monotonic() - t0) * 1000.0
                cap_error = f"capability_suppressed: {_suppression_reason}"
                cap_receipt = await self._persist_suppression_receipt(
                    event_id=event.event_id,
                    delivery_plan_id=route_plan.plan_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
                    error=cap_error,
                    source=source,
                    replay_run_id=replay_run_id,
                )
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=route_plan.plan_id,
                    status="skipped",
                    failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
                    receipt=cap_receipt,
                    error=cap_error,
                    duration_ms=elapsed,
                )

            # ── Phase 2.75: Plan-level skip (no state mutation) ────────

            # Plan-level skip: when the delivery plan's primary strategy
            # is "skip", produce a suppressed/skipped DeliveryOutcome
            # BEFORE capacity acquisition, outbox creation, rendering,
            # and success accounting.  This is the canonical skip path;
            # the defense-in-depth skip inside deliver_to_target() exists
            # only for direct calls that bypass _deliver_one().
            #
            # IMPORTANT: Only apply plan-level skip for adapters that
            # are actually registered, mirroring the Phase 2.5 capability
            # guard.  Unknown / missing adapters must NOT be classified as
            # CAPABILITY_SUPPRESSED here — they need to fall through to
            # deliver_to_target() which produces the correct ADAPTER_MISSING
            # permanent failure with a meaningful error message.
            if (
                route_plan.primary_strategy.method == "skip"
                and adapter_id
                and adapter_id in self._config.adapters
            ):
                self._log.info(
                    "plan_skip: route_id=%s event_id=%s target_adapter=%s "
                    "plan_id=%s strategy_method=skip",
                    route.id,
                    event.event_id,
                    adapter_id,
                    route_plan.plan_id,
                )
                if self._route_stats is not None:
                    self._route_stats.record_capability_suppressed(route.id)
                if self._runtime_accounting is not None:
                    self._runtime_accounting.record_capability_suppressed()
                elapsed = (time.monotonic() - t0) * 1000.0
                skip_error = (
                    f"plan_skip: delivery strategy is 'skip' "
                    f"(event_kind={event.event_kind})"
                )
                skip_receipt = await self._persist_suppression_receipt(
                    event_id=event.event_id,
                    delivery_plan_id=route_plan.plan_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
                    error=skip_error,
                    source=source,
                    replay_run_id=replay_run_id,
                )
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=route_plan.plan_id,
                    status="skipped",
                    failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
                    receipt=skip_receipt,
                    error=skip_error,
                    duration_ms=elapsed,
                )

            # ── Phase 3: Capacity acquisition ────────────────────────

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
                    # Persist lightweight suppression evidence so operators
                    # can inspect capacity/shutdown rejections via receipts.
                    suppression_receipt = await self._persist_suppression_receipt(
                        event_id=event.event_id,
                        delivery_plan_id=(
                            route_plan.plan_id if hasattr(route_plan, "plan_id") else ""
                        ),
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        failure_kind=capacity_failure_kind,
                        error=capacity_error,
                        source=source,
                        replay_run_id=replay_run_id,
                    )
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
                        receipt=suppression_receipt,
                        error=capacity_error,
                        duration_ms=elapsed,
                    )

            # ── Phase 3.5: Outbox creation ─────────────────────────

            # Create a durable outbox item tracking this delivery attempt.
            # The outbox is created AFTER route/policy/loop/capacity acceptance
            # and BEFORE the adapter delivery attempt, so that pending work
            # survives a crash between this point and the receipt commit.
            _outbox_id, _outbox_created, _pipeline_worker = (
                await self._create_outbox_for_delivery(
                    event, route, route_plan, target, adapter_id
                )
            )

            # ── Phase 3.75: Lease renewal background task ────────────

            # Start a background task that periodically renews the outbox
            # lease during long adapter deliveries (e.g. radio-based
            # transports).  The renewal task is cancelled in the finally
            # block after the delivery attempt completes.
            _renewal_task: asyncio.Task | None = self._start_outbox_lease_renewal(
                _outbox_id, _outbox_created, _pipeline_worker
            )

            # ── Phase 4: Inflight tracking + delivery ────────────────

            # Compute tracking key outside try to satisfy static analysis.
            _inflight_key: str = (
                f"{event.event_id}:{route.id}:{adapter_id}:{route_plan.plan_id}"
            )
            # Track outcome for outbox update — declared here so the outer
            # finally block always sees them (e.g. on CancelledError propagate).
            _outcome_receipt: DeliveryReceipt | None = None
            _outcome_failure_kind_val: DeliveryFailureKind | None = None
            _outcome_error: str | None = None
            try:
                # Track in-flight delivery identity for shutdown evidence.
                if self._capacity_controller is not None:
                    self._inflight_deliveries[_inflight_key] = InflightDelivery(
                        event_id=event.event_id,
                        route_id=route.id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        delivery_plan_id=route_plan.plan_id,
                        source=source,
                        replay_run_id=replay_run_id,
                        acquired_at=t0,
                        outbox_id=_outbox_id,
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
                    _outcome_receipt = receipt
                    elapsed = (time.monotonic() - t0) * 1000.0
                    if self._route_stats is not None:
                        self._route_stats.record_delivered(route.id)
                    if self._runtime_accounting is not None:
                        self._runtime_accounting.record_outbound_delivered()
                    # When the adapter returned delivery_status="enqueued",
                    # the receipt has status="queued" — expose that in
                    # DeliveryOutcome so callers can distinguish local
                    # acceptance from confirmed delivery.
                    delivery_status: Literal["success", "queued"] = (
                        "queued" if receipt.status == "queued" else "success"
                    )
                    return DeliveryOutcome(
                        event_id=event.event_id,
                        target_adapter=adapter_id,
                        target_channel=target.channel,
                        route_id=route.id,
                        delivery_plan_id=route_plan.plan_id,
                        status=delivery_status,
                        failure_kind=None,
                        receipt=receipt,
                        error=None,
                        duration_ms=elapsed,
                    )
                except _AdapterDeliveryError as exc:
                    elapsed = (time.monotonic() - t0) * 1000.0
                    _outcome_receipt = exc.receipt
                    _outcome_error = exc.error
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
                    _outcome_failure_kind_val = failure_kind
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
                    _outcome_receipt = exc.receipt
                    _outcome_error = exc.error
                    _resolved_failure_kind = (
                        exc.failure_kind
                        if exc.failure_kind is not None
                        else DeliveryFailureKind.RENDERER_FAILURE
                    )
                    _outcome_failure_kind_val = _resolved_failure_kind
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
                        failure_kind=_resolved_failure_kind,
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
                    _outcome_error = f"{type(exc).__name__}: {exc}"
                    exc_type = type(exc)
                    failure_kind = RetryExecutor.classify_failure(
                        exc,
                        adapter_registered=(adapter_id in self._config.adapters),
                    )
                    _outcome_failure_kind_val = failure_kind
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
                # Stop lease renewal.
                if _renewal_task is not None:
                    _renewal_task.cancel()
                    try:
                        await _renewal_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        self._log.debug(
                            "Outbox lease renewal task ended with error",
                            exc_info=True,
                        )

                # Update outbox based on delivery outcome.
                await self._finalize_outbox_outcome(
                    _outbox_id,
                    _outbox_created,
                    _outcome_receipt,
                    _outcome_failure_kind_val,
                    _outcome_error,
                    route_plan.retry_policy,
                )

                # Untrack in-flight delivery identity.
                if self._capacity_controller is not None:
                    self._inflight_deliveries.pop(_inflight_key, None)
                    await self._capacity_controller.release_delivery()

        return list(
            await asyncio.gather(*[_deliver_one(r, p) for r, p in route_targets])
        )

    # ------------------------------------------------------------------
    # Outbox helpers (extracted from _deliver_one for readability)
    # ------------------------------------------------------------------

    async def _create_outbox_for_delivery(
        self,
        event: CanonicalEvent,
        route: Route,
        route_plan: DeliveryPlan,
        target: RouteTarget,
        adapter_name: str,
    ) -> tuple[str | None, bool, str]:
        """Create a durable outbox item tracking a delivery attempt.

        Returns ``(outbox_id, outbox_created, pipeline_worker)``.
        On failure the outbox_id is ``None`` and ``outbox_created`` is
        ``False`` — the pipeline continues without outbox tracking.
        """
        outbox_id: str | None = None
        outbox_created: bool = False
        pipeline_worker: str = ""
        try:
            _now = datetime.now(timezone.utc)
            pipeline_worker = f"pipeline:{uuid.uuid4().hex[:12]}"
            _lease_until = (
                _now + timedelta(seconds=_OUTBOX_RENEWAL_DURATION_SECONDS)
            ).isoformat()
            outbox_item = DeliveryOutboxItem(
                outbox_id=f"obox-{uuid.uuid4()}",
                event_id=event.event_id,
                route_id=route.id,
                delivery_plan_id=route_plan.plan_id,
                target_adapter=adapter_name,
                target_channel=target.channel,
                target_address=(
                    target.destination.destination_hash if target.destination else None
                ),
                attempt_number=1,
                status="in_progress",
                locked_at=_now.isoformat(),
                lease_until=_lease_until,
                worker_id=pipeline_worker,
            )
            created = await self._config.storage.create_outbox_item(outbox_item)
            outbox_id = created.outbox_id
            # create_outbox_item may return an existing non-terminal row;
            # always use the persisted owner for lease renewals.
            pipeline_worker = created.worker_id or pipeline_worker
            outbox_created = True
        except Exception:
            self._log.exception(
                "Failed to create outbox item for event_id=%s adapter=%s",
                event.event_id,
                adapter_name,
            )
            # Non-fatal: pipeline continues without outbox tracking.
        return outbox_id, outbox_created, pipeline_worker

    def _start_outbox_lease_renewal(
        self,
        outbox_id: str | None,
        outbox_created: bool,
        pipeline_worker: str,
    ) -> asyncio.Task | None:
        """Start a background task that periodically renews the outbox lease.

        Returns the :class:`asyncio.Task` managing the renewal loop, or
        ``None`` if no outbox item was created.
        """

        async def _renew_lease() -> None:
            while True:
                await asyncio.sleep(_OUTBOX_RENEWAL_INTERVAL_SECONDS)
                if outbox_id is not None:
                    try:
                        _new_lease = (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=_OUTBOX_RENEWAL_DURATION_SECONDS)
                        ).isoformat()
                        renewed = await self._config.storage.renew_outbox_lease(
                            outbox_id, pipeline_worker, _new_lease
                        )
                    except Exception:
                        self._log.exception(
                            "Transient error renewing outbox lease for %s; "
                            "will retry on next cycle",
                            outbox_id,
                        )
                        continue
                    if not renewed:
                        # Item is no longer ours — stop renewing.
                        break

        if outbox_id is not None and outbox_created:
            return asyncio.create_task(_renew_lease())
        return None

    async def _finalize_outbox_outcome(
        self,
        outbox_id: str | None,
        outbox_created: bool,
        receipt: DeliveryReceipt | None,
        failure_kind_val: DeliveryFailureKind | None,
        error: str | None,
        retry_policy: RetryPolicy | None,
    ) -> None:
        """Update the outbox item status based on the delivery outcome.

        Handles the queued / sent / retry_wait / dead_lettered state
        transitions.  Silently skips when no outbox item was created.
        """
        if outbox_id is None or not outbox_created:
            return
        try:
            if receipt is not None and receipt.status not in ("failed",):
                _r_status = receipt.status
                if _r_status == "queued":
                    await self._config.storage.mark_outbox_queued(
                        outbox_id,
                        receipt_id=receipt.receipt_id,
                        attempt_number=receipt.attempt_number,
                    )
                else:
                    await self._config.storage.mark_outbox_sent(
                        outbox_id,
                        receipt_id=receipt.receipt_id,
                        attempt_number=receipt.attempt_number,
                    )
            elif failure_kind_val is not None:
                _rec_id: str | None = (
                    receipt.receipt_id if receipt is not None else None
                )
                _att: int | None = (
                    receipt.attempt_number if receipt is not None else None
                )
                if failure_kind_val.is_retryable:
                    if retry_policy is None:
                        # No retry policy — treat as terminal.
                        await self._config.storage.mark_outbox_dead_lettered(
                            outbox_id,
                            receipt_id=_rec_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=(error[:512] if error else None),
                        )
                    else:
                        _attempt = _att or 1
                        _backoff = RetryExecutor(retry_policy).compute_backoff(_attempt)
                        _next_at = (datetime.now(timezone.utc) + _backoff).isoformat()
                        await self._config.storage.mark_outbox_retry_wait(
                            outbox_id,
                            next_attempt_at=_next_at,
                            receipt_id=_rec_id,
                            failure_kind=failure_kind_val.value,
                            error_summary=(error[:512] if error else None),
                            attempt_number=_attempt,
                        )
                else:
                    await self._config.storage.mark_outbox_dead_lettered(
                        outbox_id,
                        receipt_id=_rec_id,
                        failure_kind=failure_kind_val.value,
                        error_summary=(error[:512] if error else None),
                    )
        except Exception:
            self._log.exception(
                "Failed to update outbox %s after delivery",
                outbox_id,
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
                receipt=receipt,
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
                receipt=receipt,
            ) from None

        # Render the event into a RenderingResult before adapter delivery.
        # Pass the adapter's platform so renderers can match on platform
        # identity instead of adapter-name heuristics.
        # Enrich relations with target-adapter native refs so that the
        # renderer (and downstream adapter) receive native IDs for
        # structured replies / reactions.  This enrichment is per-target
        # and does not mutate the stored original event.
        render_event = await self._enrich_relations_for_target(
            event, adapter_id or "", target.channel
        )
        target_platform = getattr(adapter, "platform", None)
        if isinstance(target_platform, str):
            platform_param: str | None = target_platform
        else:
            platform_param = None
        # Resolve adapter capabilities to pass text budgets to renderers.
        _caps = self._get_adapter_capabilities(target)
        _max_text_chars = _caps.max_text_chars
        _max_text_bytes = _caps.max_text_bytes

        # Honor the delivery plan's strategy: validate and narrow the
        # method string to a typed DeliveryStrategyMethod before passing
        # it to the rendering pipeline.
        _strategy_method = plan.primary_strategy.method

        if _strategy_method == "skip":
            # Defense-in-depth only: the canonical skip path is in
            # _deliver_one() Phase 2.75 which runs BEFORE outbox
            # creation, capacity acquisition, and rendering.  This block
            # handles edge cases where deliver_to_target() is called
            # directly (e.g. via _deliver_all).  A plan-level skip is
            # NOT a renderer failure — it is a suppressed delivery.
            _skip_error = (
                f"delivery_skipped: plan strategy is 'skip' "
                f"(event_kind={event.event_kind})"
            )
            _skip_receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="suppressed",
                error=_skip_error,
                failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED.value,
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
            await self._config.storage.append_receipt(_skip_receipt)
            return _skip_receipt

        # Validate the strategy method against the strict
        # DeliveryStrategyMethod literal type accepted by
        # RenderingPipeline.render().  Unknown methods are pipeline
        # configuration errors — the strategy string is invalid before
        # any rendering is attempted.
        try:
            _validated_strategy: DeliveryStrategyMethod = _validate_strategy_method(
                _strategy_method
            )
        except ValueError:
            _invalid_error = (
                f"Invalid delivery strategy method "
                f"{_strategy_method!r}: not a known strategy"
            )
            self._diagnostician.record_planner_failure(event.event_id, _invalid_error)
            receipt = DeliveryReceipt(
                sequence=0,
                receipt_id=receipt_id,
                event_id=event.event_id,
                delivery_plan_id=plan.plan_id,
                target_adapter=adapter_id or "",
                target_channel=target.channel,
                route_id=route.id,
                status="failed",
                error=_invalid_error,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE.value,
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
            raise _RendererDeliveryError(
                adapter_id or "",
                _invalid_error,
                receipt=receipt,
                failure_kind=DeliveryFailureKind.PLANNER_FAILURE,
            ) from None

        try:
            rendering_result = await self._rendering_pipeline.render(
                render_event,
                adapter_id or "",
                target.channel,
                target_platform=platform_param,
                max_text_chars=_max_text_chars,
                max_text_bytes=_max_text_bytes,
                delivery_strategy=_validated_strategy,
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
            raise _RendererDeliveryError(
                adapter_id or "", rendering_error, receipt=receipt
            ) from None

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
                receipt=receipt,
            ) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        adapter_result: AdapterDeliveryResult | None = None
        try:
            adapter_result = await deliver_fn(rendering_result)

            # Respect the adapter's declared delivery lifecycle state.
            # Queue-based adapters return
            # delivery_status="enqueued" to indicate local acceptance
            # only; synchronous adapters use the default "sent".
            _adapter_delivery_status = (
                getattr(adapter_result, "delivery_status", "sent")
                if adapter_result
                else "sent"
            )
            status: Literal["sent", "failed", "queued"] = (
                "queued" if _adapter_delivery_status == "enqueued" else "sent"
            )
            error: str | None = None
            _log_status = _adapter_delivery_status
            self._log.info(
                "Delivered: event_id=%s → adapter=%s plan=%s attempt=%d " "status=%s",
                event.event_id,
                adapter_id,
                plan.plan_id,
                attempt_number,
                _log_status,
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

        # Populate adapter_message_id only when delivery succeeded and
        # the adapter returned a native_message_id.  Never fabricate IDs.
        _adapter_message_id: str | None = None
        if (
            status == "sent"
            and adapter_result is not None
            and adapter_result.native_message_id is not None
        ):
            _adapter_message_id = adapter_result.native_message_id

        # Serialize rendering evidence from the rendering result into the
        # receipt.  Only attached on successful deliveries (sent / queued);
        # suppressed, skipped, rendering-failure, and adapter-failure receipts
        # naturally leave rendering_evidence=None.  Uses getattr for forward
        # compatibility when RenderingResult.rendering_evidence has not yet
        # been added by the parallel evidence model task.
        _rendering_evidence: str | None = None
        if status in ("sent", "queued"):
            _raw_evidence = getattr(rendering_result, "rendering_evidence", None)
            if _raw_evidence is not None:
                _rendering_evidence = (
                    _raw_evidence
                    if isinstance(_raw_evidence, str)
                    else msgspec.json.encode(_raw_evidence).decode()
                )

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
            adapter_message_id=_adapter_message_id,
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
            rendering_evidence=_rendering_evidence,
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
                adapter_id or "", error or "", delivery_exc, receipt=receipt
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

    def _get_adapter_capabilities(self, target: RouteTarget) -> AdapterCapabilities:
        """Retrieve the :class:`AdapterCapabilities` for a target adapter.

        Delegates to :func:`~medre.core.planning.capabilities.resolve_adapter_capabilities`
        with the configured adapter registry.  When the adapter is missing
        from the registry (yields ``None``), falls back to a default
        :class:`AdapterCapabilities` for backward compatibility — the
        pipeline has its own adapter-missing check at Phase 2.5.
        """
        caps = resolve_adapter_capabilities(self._config.adapters, target)
        if caps is None:
            return AdapterCapabilities()
        return caps
