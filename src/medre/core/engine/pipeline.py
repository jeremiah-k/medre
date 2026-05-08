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
from typing import TYPE_CHECKING, Any, Callable, Awaitable, Literal

from medre.adapters.base import AdapterCapabilities, BaseAdapter
from medre.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.events.bus import EventBus, EventMiddleware
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import DeliveryOutcome, DeliveryPlan
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline, RenderingResult
from medre.core.rendering.text import TextRenderer
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.router import Router
from medre.core.storage.backend import StorageBackend

if TYPE_CHECKING:
    pass


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

    Carries the adapter ID, error string, and the original exception so
    that callers can classify the failure as transient or permanent.
    """

    def __init__(
        self, adapter_id: str, error: str, original: Exception | None = None
    ) -> None:
        self.adapter_id = adapter_id
        self.error = error
        self.original = original
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

    # -- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Register pipeline middleware with the event bus.

        Call this before any adapter calls :attr:`ingress_handler`.
        """
        self._middleware = _PipelineLoggingMiddleware()
        self._config.event_bus.add_middleware(self._middleware, priority=100)
        self._log.info("PipelineRunner started")

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
        2. Store the event.
        3. Route the event and create delivery plans.
        4. Deliver to each target independently.

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

        # Stage 2 – store
        await self.store_event(event)

        # Stages 3-4 – route, plan, deliver
        try:
            deliveries = await self.route_event(event)
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

    # -- Stage 3-4: Routing + Planning -------------------------------------

    async def route_event(
        self,
        event: CanonicalEvent,
    ) -> list[tuple[Route, DeliveryPlan]]:
        """Match *event* against routes and produce delivery plans.

        For each matched route, resolves its targets and creates a
        :class:`DeliveryPlan` per target using the fallback resolver.

        Parameters
        ----------
        event:
            The canonical event to route.

        Returns
        -------
        list[tuple[Route, DeliveryPlan]]
            Paired routes and their per-target delivery plans.
        """
        matched_routes = self._config.router.match(event)

        if not matched_routes:
            self._log.debug(
                "No routes matched for event_id=%s kind=%s",
                event.event_id,
                event.event_kind,
            )
            return []

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

        return results

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

        async def _deliver_one(
            route: Route, plan: DeliveryPlan
        ) -> DeliveryOutcome:
            target = plan.target
            adapter_id = target.adapter or ""
            t0 = time.monotonic()
            try:
                receipt = await self.deliver_to_target(event, route, plan)
                elapsed = (time.monotonic() - t0) * 1000.0
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="success",
                    receipt=receipt,
                    error=None,
                    duration_ms=elapsed,
                )
            except _AdapterDeliveryError as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                self._diagnostician.record_adapter_failure(
                    event.event_id, adapter_id, exc.error
                )
                # Classify based on the original adapter exception.
                if exc.original is not None:
                    outcome_status = self._classify_adapter_error(exc.original)
                else:
                    outcome_status = "transient_failure"
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status=outcome_status,
                    receipt=None,
                    error=exc.error,
                    duration_ms=elapsed,
                )
            except _RendererDeliveryError as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status="permanent_failure",
                    receipt=None,
                    error=exc.error,
                    duration_ms=elapsed,
                )
            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000.0
                exc_type = type(exc)
                status = self._classify_adapter_error(exc)
                error_msg = f"{exc_type.__name__}: {exc}"
                self._diagnostician.record_adapter_failure(
                    event.event_id, adapter_id, error_msg
                )
                return DeliveryOutcome(
                    event_id=event.event_id,
                    target_adapter=adapter_id,
                    target_channel=target.channel,
                    route_id=route.id,
                    delivery_plan_id=plan.plan_id,
                    status=status,
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

        Transient failures are retryable (timeouts, connection errors,
        temporary OS-level issues).  All other exceptions are treated as
        permanent failures.

        Parameters
        ----------
        exc:
            The exception raised by the adapter.

        Returns
        -------
        str
            ``"transient_failure"`` or ``"permanent_failure"``.
        """
        transient_types = (
            TimeoutError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            OSError,
        )
        if isinstance(exc, transient_types):
            return "transient_failure"
        return "permanent_failure"

    async def deliver_to_target(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
    ) -> DeliveryReceipt:
        """Deliver *event* to a single target adapter and record the receipt.

        Steps:

        1. Look up the target adapter from the config.
        2. Call the adapter's ``deliver`` method.
        3. Record a :class:`DeliveryReceipt` in storage.
        4. Store a :class:`NativeMessageRef` mapping.

        Parameters
        ----------
        event:
            The canonical event to deliver.
        route:
            The route that matched the event.
        plan:
            The delivery plan for this target.

        Returns
        -------
        DeliveryReceipt
            The receipt recording the delivery outcome.
        """
        target = plan.target
        adapter_id = target.adapter
        now = datetime.now(tz=timezone.utc)
        receipt_id = f"rcpt-{uuid.uuid4()}"

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
                status="failed",
                error=f"Adapter {adapter_id!r} not registered",
                created_at=now,
            )
            await self._config.storage.append_receipt(receipt)
            return receipt

        # Render the event into a RenderingResult before adapter delivery.
        try:
            rendering_result = await self._rendering_pipeline.render(
                event, adapter_id or "", target.channel,
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
                status="failed",
                error=rendering_error,
                created_at=now,
            )
            await self._config.storage.append_receipt(receipt)
            raise _RendererDeliveryError(adapter_id or "", rendering_error) from None

        # Deliver the rendered result via adapter.
        delivery_exc: Exception | None = None
        try:
            deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
            if deliver_fn is not None and callable(deliver_fn):
                await deliver_fn(rendering_result)
            else:
                self._log.warning(
                    "Adapter %r has no deliver() method; skipping delivery",
                    adapter_id,
                )

            status: str = "sent"
            error: str | None = None
            self._log.info(
                "Delivered: event_id=%s → adapter=%s plan=%s",
                event.event_id,
                adapter_id,
                plan.plan_id,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            delivery_exc = exc
            self._log.exception(
                "Delivery failed: event_id=%s → adapter=%s",
                event.event_id,
                adapter_id,
            )

        # Record receipt.
        receipt = DeliveryReceipt(
            sequence=0,
            receipt_id=receipt_id,
            event_id=event.event_id,
            delivery_plan_id=plan.plan_id,
            target_adapter=adapter_id or "",
            status=status,  # type: ignore[arg-type]
            error=error,
            created_at=now,
        )
        await self._config.storage.append_receipt(receipt)

        # Store native ref mapping (outbound direction).
        native_ref = NativeMessageRef(
            id=f"nref-{uuid.uuid4()}",
            event_id=event.event_id,
            adapter=adapter_id or "",
            native_channel_id=target.channel,
            native_message_id=f"native-{event.event_id}",
            native_thread_id=None,
            native_relation_id=None,
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
