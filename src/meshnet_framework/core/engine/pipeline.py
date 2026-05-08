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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Awaitable

from meshnet_framework.adapters.base import AdapterCapabilities, BaseAdapter
from meshnet_framework.core.events.canonical import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from meshnet_framework.core.events.bus import EventBus, EventMiddleware
from meshnet_framework.core.planning.delivery_plan import DeliveryPlan
from meshnet_framework.core.planning.fallback_resolution import FallbackResolver
from meshnet_framework.core.planning.relation_resolution import RelationResolver
from meshnet_framework.core.routing.models import Route, RouteTarget
from meshnet_framework.core.routing.router import Router
from meshnet_framework.core.storage.backend import StorageBackend

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
    logger:
        Optional logger override; defaults to the module logger.
    """

    storage: StorageBackend
    router: Router
    fallback_resolver: FallbackResolver
    relation_resolver: RelationResolver
    adapters: dict[str, BaseAdapter]
    event_bus: EventBus
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

    async def handle_ingress(self, event: CanonicalEvent) -> None:
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
        deliveries = await self.route_event(event)

        if not deliveries:
            self._log.info(
                "No routes matched for event_id=%s", event.event_id
            )
            return

        # Deliver to all targets concurrently with error isolation.
        results = await self._deliver_all(event, deliveries)

        self._log.info(
            "Pipeline complete: event_id=%s targets=%d succeeded=%d failed=%d",
            event.event_id,
            len(deliveries),
            sum(1 for r in results if r is not None),
            sum(1 for r in results if r is None),
        )

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

        # Deliver via adapter.
        try:
            deliver_fn: Callable[..., Any] | None = getattr(adapter, "deliver", None)
            if deliver_fn is not None and callable(deliver_fn):
                await deliver_fn(event)
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
