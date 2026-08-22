"""Per-target delivery coordination for :mod:`medre.core.engine.pipeline`.

This module owns orchestration only.  It sequences already-authoritative
components for one delivery target:

* replay/loop/policy/capability/plan preflight checks;
* runtime delivery-capacity acquisition and release;
* durable outbox ownership and lease renewal via :class:`OutboxManager`;
* in-flight identity tracking for shutdown evidence;
* target execution through the runner-provided delivery callback; and
* outcome accounting/normalisation around the target-delivery service.

It does not decide routing, rendering semantics, retry policy, receipt lifecycle,
or outbox transitions.  Those remain owned by the router/planner,
``TargetDeliveryService``, ``DeliveryLifecycleService``, and ``OutboxManager``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Protocol, TypeVar, cast

from medre.core.contracts.adapter import AdapterContract
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxContext, OutboxManager
from medre.core.engine.pipeline.target_delivery import (
    _AdapterDeliveryError,
    _RendererDeliveryError,
)
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt, NativeMessageRef
from medre.core.observability.correlation import correlation_scope
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryOutcome,
    DeliveryPlan,
)
from medre.core.policies.route_policy import BLOCKED_VALUE_CUTOFF, evaluate_route_policy
from medre.core.routing.models import Route, RouteTarget
from medre.core.routing.stats import RouteStats
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting

if TYPE_CHECKING:
    from medre.core.supervision.capacity import CapacityController

_ItemT = TypeVar("_ItemT")
_ResultT = TypeVar("_ResultT")

_GetEventFn = Callable[[str], Awaitable[CanonicalEvent | None]]
_ListNativeRefsFn = Callable[[str], Awaitable[list[NativeMessageRef]]]


class _PersistSuppressionReceiptFn(Protocol):
    async def __call__(
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
    ) -> DeliveryReceipt: ...


class _DeliverTargetFn(Protocol):
    async def __call__(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        previous_receipt: DeliveryReceipt | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
        cached_get_fn: _GetEventFn | None = None,
        cached_list_fn: _ListNativeRefsFn | None = None,
        outbox_id: str | None = None,
    ) -> DeliveryReceipt: ...


async def _bounded_ordered_map(
    items: list[_ItemT],
    handler: Callable[[_ItemT], Awaitable[_ResultT]],
    *,
    worker_limit: int,
) -> list[_ResultT]:
    """Apply *handler* with bounded task creation and stable result ordering.

    Per-item failures are retained until every sibling item finishes, then the
    first failure in input order is re-raised unchanged.  Cancellation of the
    caller still cancels the worker group immediately.
    """
    if not items:
        return []
    worker_count = min(max(1, worker_limit), len(items))
    sentinel = object()
    results: list[object] = [sentinel] * len(items)
    next_index = 0
    index_lock = asyncio.Lock()

    async def _worker() -> None:
        nonlocal next_index
        while True:
            async with index_lock:
                if next_index >= len(items):
                    return
                index = next_index
                next_index += 1
                item = items[index]
            try:
                results[index] = await handler(item)
            except asyncio.CancelledError as exc:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                # A handler may raise CancelledError directly even though the
                # worker itself was not cancelled. TaskGroup otherwise treats
                # that as normal child cancellation and loses the exception.
                results[index] = exc
            except Exception as exc:
                results[index] = exc

    async with asyncio.TaskGroup() as task_group:
        for _ in range(worker_count):
            task_group.create_task(_worker())

    if any(result is sentinel for result in results):
        raise RuntimeError("bounded worker pool returned incomplete results")
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cast(list[_ResultT], results)


@dataclass(frozen=True)
class InflightDelivery:
    """Identity of one capacity-owned adapter delivery in progress.

    ``MedreApp.stop()`` drains these records after the shared runtime deadline
    expires so shutdown can persist abandonment evidence without asking the
    adapter or delivery service to reconstruct runtime ownership.
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


@dataclass(frozen=True)
class _DeliveryContext:
    """Immutable inputs shared by all phases of one target delivery."""

    event: CanonicalEvent
    route: Route
    plan: DeliveryPlan
    source: str
    replay_run_id: str | None
    cached_get_fn: _GetEventFn | None
    cached_list_fn: _ListNativeRefsFn | None
    started_at: float

    @property
    def target(self) -> RouteTarget:
        return self.plan.target

    @property
    def adapter_id(self) -> str:
        return self.target.adapter or ""

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000.0


@dataclass(frozen=True)
class _ExecutionResult:
    """Delivery outcome plus the evidence needed for outbox finalization."""

    outcome: DeliveryOutcome
    receipt: DeliveryReceipt | None
    failure_kind: DeliveryFailureKind | None
    error: str | None


class DeliveryCoordinator:
    """Coordinate per-target delivery without owning semantic authorities.

    The coordinator exists to make resource ownership and phase ordering
    explicit.  In particular, after capacity acquisition there is one cleanup
    boundary that always releases capacity and removes in-flight identity, even
    when outbox creation is cancelled or durable outbox finalization fails.
    """

    def __init__(
        self,
        *,
        storage: StorageBackend,
        adapters: Mapping[str, AdapterContract],
        lifecycle: DeliveryLifecycleService,
        outbox_manager: OutboxManager,
        diagnostician: Diagnostician,
        persist_suppression_receipt: _PersistSuppressionReceiptFn,
        deliver_target: _DeliverTargetFn,
        inflight_deliveries: MutableMapping[str, InflightDelivery],
        record_capacity_rejection: Callable[[], None],
        logger: logging.Logger,
        route_stats: RouteStats | None,
        runtime_accounting: RuntimeAccounting | None,
    ) -> None:
        self._storage = storage
        self._adapters = adapters
        self._lifecycle = lifecycle
        self._outbox_manager = outbox_manager
        self._diagnostician = diagnostician
        self._persist_suppression_receipt = persist_suppression_receipt
        self._deliver_target = deliver_target
        self._inflight_deliveries = inflight_deliveries
        self._record_capacity_rejection = record_capacity_rejection
        self._log = logger
        self._route_stats = route_stats
        self._runtime_accounting = runtime_accounting
        self._capacity_controller: CapacityController | None = None

    def set_capacity_controller(self, controller: CapacityController) -> None:
        """Use *controller* for delivery admission and release."""
        self._capacity_controller = controller

    async def deliver_many(
        self,
        event: CanonicalEvent,
        route_targets: list[tuple[Route, DeliveryPlan]],
        *,
        source: str = "live",
        replay_run_id: str | None = None,
        cached_get_fn: _GetEventFn | None = None,
        cached_list_fn: _ListNativeRefsFn | None = None,
    ) -> list[DeliveryOutcome]:
        """Deliver to targets with bounded task creation and stable ordering."""
        if not route_targets:
            return []

        worker_limit = (
            self._capacity_controller.delivery_limit
            if self._capacity_controller is not None
            else 1
        )

        async def _deliver_item(
            item: tuple[Route, DeliveryPlan],
        ) -> DeliveryOutcome:
            route, plan = item
            return await self._deliver_one(
                event,
                route,
                plan,
                source=source,
                replay_run_id=replay_run_id,
                cached_get_fn=cached_get_fn,
                cached_list_fn=cached_list_fn,
            )

        return await _bounded_ordered_map(
            route_targets,
            _deliver_item,
            worker_limit=worker_limit,
        )

    async def _deliver_one(
        self,
        event: CanonicalEvent,
        route: Route,
        plan: DeliveryPlan,
        *,
        source: str,
        replay_run_id: str | None,
        cached_get_fn: _GetEventFn | None,
        cached_list_fn: _ListNativeRefsFn | None,
    ) -> DeliveryOutcome:
        ctx = _DeliveryContext(
            event=event,
            route=route,
            plan=plan,
            source=source,
            replay_run_id=replay_run_id,
            cached_get_fn=cached_get_fn,
            cached_list_fn=cached_list_fn,
            started_at=time.monotonic(),
        )
        with correlation_scope(
            trace_id=event.trace_id,
            event_id=event.event_id,
            conversation_id=event.conversation_id,
            route_id=route.id,
            delivery_plan_id=plan.plan_id,
            target_adapter=ctx.target.adapter,
            source=source,
            replay_run_id=replay_run_id,
        ):
            return await self._deliver_one_scoped(ctx)

    async def _deliver_one_scoped(self, ctx: _DeliveryContext) -> DeliveryOutcome:
        replay_receipts = await self._load_replay_receipts(ctx)
        preflight = await self._preflight_outcome(ctx, replay_receipts)
        if preflight is not None:
            return preflight

        owned_controller, capacity_rejection = (
            await self._acquire_capacity_or_reject(ctx)
        )
        if capacity_rejection is not None:
            return capacity_rejection

        # From this point onward ``owned_controller`` is the exact controller
        # whose slot this coroutine acquired.  Snapshotting ownership prevents
        # later wiring changes from releasing a slot this attempt never owned.
        # One outer finally spans outbox creation, target execution, and
        # outbox-finalization failure.
        inflight_key: str | None = None
        try:
            outbox_ctx = await self._outbox_manager.create_for_delivery(
                ctx.event,
                ctx.route,
                ctx.plan,
                ctx.target,
                ctx.adapter_id,
                source=ctx.source,
            )
            if outbox_ctx.skip_reason is not None:
                return self._build_outcome(
                    ctx,
                    status="skipped",
                    failure_kind=DeliveryFailureKind.OUTBOX_NOT_OWNED,
                    error=f"outbox row not owned: {outbox_ctx.skip_reason}",
                    failure_kind_detail=outbox_ctx.skip_reason,
                )

            inflight_key = (
                self._inflight_key(ctx) if owned_controller is not None else None
            )
            return await self._execute_owned_delivery(
                ctx,
                replay_receipts=replay_receipts,
                outbox_ctx=outbox_ctx,
                inflight_key=inflight_key,
            )
        finally:
            if owned_controller is not None:
                if inflight_key is not None:
                    self._inflight_deliveries.pop(inflight_key, None)
                await owned_controller.release_delivery()

    async def _load_replay_receipts(
        self,
        ctx: _DeliveryContext,
    ) -> list[DeliveryReceipt]:
        if ctx.source != "replay":
            return []
        try:
            return await self._storage.list_receipts_for_event(ctx.event.event_id)
        except Exception:
            # Same-run suppression is a safety boundary: if a non-empty run ID
            # cannot be checked, fail closed rather than duplicate delivery.
            # Empty run IDs remain best-effort.
            if ctx.replay_run_id:
                raise
            self._log.debug(
                "Failed to load replay receipt history; proceeding without "
                "attempt lineage: event_id=%s adapter=%s",
                ctx.event.event_id,
                ctx.adapter_id,
                exc_info=True,
            )
            return []

    async def _preflight_outcome(
        self,
        ctx: _DeliveryContext,
        replay_receipts: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        """Run suppression checks in the normative pre-capacity order."""
        for check in (
            self._replay_duplicate_outcome,
            self._route_trace_loop_outcome,
            self._self_loop_outcome,
            self._policy_outcome,
            self._capability_outcome,
            self._plan_skip_outcome,
        ):
            outcome = await check(ctx, replay_receipts)
            if outcome is not None:
                return outcome
        return None

    async def _replay_duplicate_outcome(
        self,
        ctx: _DeliveryContext,
        replay_receipts: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        if not ctx.replay_run_id:
            return None
        prior_accepted = any(
            receipt.source == "replay"
            and receipt.replay_run_id == ctx.replay_run_id
            and receipt.delivery_plan_id == ctx.plan.plan_id
            and receipt.target_adapter == ctx.adapter_id
            and (receipt.target_channel or None) == (ctx.target.channel or None)
            and receipt.status in {"queued", "sent"}
            for receipt in replay_receipts
        )
        if not prior_accepted:
            return None
        error = "replay_duplicate_suppressed: run target already accepted"
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.REPLAY_DUPLICATE_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.REPLAY_DUPLICATE_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _route_trace_loop_outcome(
        self,
        ctx: _DeliveryContext,
        _: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        routing_meta = ctx.event.metadata.routing
        if routing_meta is None:
            return None
        trace_count = sum(1 for route_id in routing_meta.route_trace if route_id == ctx.route.id)
        if trace_count <= 1:
            return None
        self._log.warning(
            "loop_prevented: route_id=%s already in route_trace for "
            "event_id=%s (trace=%s)",
            ctx.route.id,
            ctx.event.event_id,
            routing_meta.route_trace,
        )
        self._record_loop_suppressed(ctx.route.id)
        error = "loop_prevented: route already traversed in prior routing pass"
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _self_loop_outcome(
        self,
        ctx: _DeliveryContext,
        _: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        if not ctx.adapter_id or ctx.adapter_id != ctx.event.source_adapter:
            return None
        self._log.warning(
            "loop_prevented: skipping delivery of event_id=%s back to "
            "source_adapter=%s (route=%s)",
            ctx.event.event_id,
            ctx.adapter_id,
            ctx.route.id,
        )
        self._record_loop_suppressed(ctx.route.id)
        error = "loop_prevented"
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.LOOP_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _policy_outcome(
        self,
        ctx: _DeliveryContext,
        _: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        if ctx.route.policy is None:
            return None
        decision = evaluate_route_policy(ctx.route.policy, ctx.event, ctx.target)
        if decision.allowed:
            return None

        blocked_value = decision.blocked_value or ""
        if len(blocked_value) >= BLOCKED_VALUE_CUTOFF:
            blocked_value = blocked_value[:BLOCKED_VALUE_CUTOFF] + "..."
        self._log.info(
            "policy_suppressed: route_id=%s event_id=%s target_adapter=%s "
            "reason=%s blocked_field=%s blocked_value=%r",
            ctx.route.id,
            ctx.event.event_id,
            ctx.adapter_id,
            decision.reason,
            decision.blocked_field,
            blocked_value,
        )
        if self._route_stats is not None:
            self._route_stats.record_policy_suppressed(ctx.route.id)
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_policy_suppressed()
        error = (
            f"policy_suppressed: {decision.reason} "
            f"({decision.blocked_field}={blocked_value!r}); "
            f"{decision.allowed_summary}"
        )
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.POLICY_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _capability_outcome(
        self,
        ctx: _DeliveryContext,
        _: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        if not (
            ctx.adapter_id
            and ctx.adapter_id in self._adapters
            and ctx.plan.capability_level == "unsupported"
        ):
            return None
        reason = (
            ctx.plan.capability_reason
            or f"{ctx.plan.capability_field or 'capability'} unsupported"
        )
        self._log.info(
            "capability_suppressed: route_id=%s event_id=%s "
            "target_adapter=%s reason=%s",
            ctx.route.id,
            ctx.event.event_id,
            ctx.adapter_id,
            reason,
        )
        self._record_capability_suppressed(ctx.route.id)
        error = f"capability_suppressed: {reason}"
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _plan_skip_outcome(
        self,
        ctx: _DeliveryContext,
        _: list[DeliveryReceipt],
    ) -> DeliveryOutcome | None:
        if not (
            ctx.plan.primary_strategy.method == "skip"
            and ctx.adapter_id
            and ctx.adapter_id in self._adapters
        ):
            return None
        self._log.info(
            "plan_skip: route_id=%s event_id=%s target_adapter=%s "
            "plan_id=%s strategy_method=skip",
            ctx.route.id,
            ctx.event.event_id,
            ctx.adapter_id,
            ctx.plan.plan_id,
        )
        self._record_capability_suppressed(ctx.route.id)
        if ctx.plan.capability_reason:
            error = f"plan_skip: {ctx.plan.capability_reason}"
        else:
            error = (
                "plan_skip: delivery strategy is 'skip' "
                f"(event_kind={ctx.event.event_kind})"
            )
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            error=error,
        )
        return self._build_outcome(
            ctx,
            status="skipped",
            failure_kind=DeliveryFailureKind.CAPABILITY_SUPPRESSED,
            receipt=receipt,
            error=error,
        )

    async def _acquire_capacity_or_reject(
        self,
        ctx: _DeliveryContext,
    ) -> tuple[CapacityController | None, DeliveryOutcome | None]:
        controller = self._capacity_controller
        if controller is None:
            return None, None
        if await controller.acquire_delivery():
            return controller, None

        self._record_capacity_rejection()
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_capacity_rejection()
        if self._route_stats is not None:
            self._route_stats.record_failed(ctx.route.id, "delivery_capacity_exceeded")

        if not controller.accepting_work:
            failure_kind = DeliveryFailureKind.SHUTDOWN_REJECTION
            error = "delivery_rejected_shutdown"
        else:
            failure_kind = DeliveryFailureKind.CAPACITY_REJECTION
            error = "delivery_capacity_exceeded"
        receipt = await self._persist_suppression(
            ctx,
            failure_kind=failure_kind,
            error=error,
        )
        return None, self._build_outcome(
            ctx,
            status="permanent_failure",
            failure_kind=failure_kind,
            receipt=receipt,
            error=error,
        )

    async def _execute_owned_delivery(
        self,
        ctx: _DeliveryContext,
        *,
        replay_receipts: list[DeliveryReceipt],
        outbox_ctx: OutboxContext,
        inflight_key: str | None,
    ) -> DeliveryOutcome:
        renewal_task = self._outbox_manager.start_lease_renewal(outbox_ctx)
        result: _ExecutionResult | None = None
        if inflight_key is not None:
            self._inflight_deliveries[inflight_key] = InflightDelivery(
                event_id=ctx.event.event_id,
                route_id=ctx.route.id,
                target_adapter=ctx.adapter_id,
                target_channel=ctx.target.channel,
                delivery_plan_id=ctx.plan.plan_id,
                source=ctx.source,
                replay_run_id=ctx.replay_run_id,
                acquired_at=ctx.started_at,
                outbox_id=outbox_ctx.outbox_id,
            )
        try:
            result = await self._invoke_target(ctx, replay_receipts, outbox_ctx)
            return result.outcome
        finally:
            # The outbox lifecycle is finalized before capacity release.  The
            # caller's outer finally still releases capacity if finalization
            # itself raises, so persistence faults cannot leak runtime slots.
            await OutboxManager.cancel_renewal(renewal_task)
            await self._outbox_manager.finalize_outcome(
                outbox_ctx,
                result.receipt if result is not None else None,
                result.failure_kind if result is not None else None,
                result.error if result is not None else None,
                ctx.plan.retry_policy,
            )

    async def _invoke_target(
        self,
        ctx: _DeliveryContext,
        replay_receipts: list[DeliveryReceipt],
        outbox_ctx: OutboxContext,
    ) -> _ExecutionResult:
        try:
            if self._runtime_accounting is not None:
                self._runtime_accounting.record_outbound_attempt()

            previous_receipt = self._latest_matching_receipt(ctx, replay_receipts)
            receipt = await self._deliver_target(
                ctx.event,
                ctx.route,
                ctx.plan,
                previous_receipt=previous_receipt,
                source=ctx.source,
                replay_run_id=ctx.replay_run_id,
                cached_get_fn=ctx.cached_get_fn,
                cached_list_fn=ctx.cached_list_fn,
                outbox_id=outbox_ctx.outbox_id,
            )
            if self._route_stats is not None:
                self._route_stats.record_delivered(ctx.route.id)
            if self._runtime_accounting is not None:
                self._runtime_accounting.record_outbound_delivered()
            status: Literal["success", "queued"] = (
                "queued" if receipt.status == "queued" else "success"
            )
            outcome = self._build_outcome(
                ctx,
                status=status,
                receipt=receipt,
            )
            return _ExecutionResult(outcome, receipt, None, None)
        except _AdapterDeliveryError as exc:
            self._diagnostician.record_adapter_failure(
                ctx.event.event_id,
                ctx.adapter_id,
                exc.error,
            )
            self._record_failed(ctx.route.id, exc.error)
            if exc.failure_kind is not None:
                failure_kind = exc.failure_kind
            elif exc.original is not None:
                failure_kind = self._lifecycle.classify_failure(
                    exc.original,
                    adapter_registered=True,
                )
            else:
                failure_kind = DeliveryFailureKind.ADAPTER_TRANSIENT
            status: Literal["transient_failure", "permanent_failure"] = (
                "transient_failure" if failure_kind.is_retryable else "permanent_failure"
            )
            outcome = self._build_outcome(
                ctx,
                status=status,
                failure_kind=failure_kind,
                error=exc.error,
            )
            return _ExecutionResult(outcome, exc.receipt, failure_kind, exc.error)
        except _RendererDeliveryError as exc:
            failure_kind = (
                exc.failure_kind
                if exc.failure_kind is not None
                else DeliveryFailureKind.RENDERER_FAILURE
            )
            self._record_failed(ctx.route.id, exc.error)
            outcome = self._build_outcome(
                ctx,
                status="permanent_failure",
                failure_kind=failure_kind,
                error=exc.error,
            )
            return _ExecutionResult(outcome, exc.receipt, failure_kind, exc.error)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failure_kind = self._lifecycle.classify_failure(
                exc,
                adapter_registered=(ctx.adapter_id in self._adapters),
            )
            status: Literal["transient_failure", "permanent_failure"] = (
                "transient_failure"
                if failure_kind.is_retryable
                else "permanent_failure"
            )
            self._diagnostician.record_adapter_failure(
                ctx.event.event_id,
                ctx.adapter_id,
                error,
            )
            self._record_failed(ctx.route.id, error)
            outcome = self._build_outcome(
                ctx,
                status=status,
                failure_kind=failure_kind,
                error=error,
            )
            return _ExecutionResult(outcome, None, failure_kind, error)

    def _latest_matching_receipt(
        self,
        ctx: _DeliveryContext,
        replay_receipts: list[DeliveryReceipt],
    ) -> DeliveryReceipt | None:
        if ctx.source != "replay":
            return None
        matching = [
            receipt
            for receipt in replay_receipts
            if receipt.delivery_plan_id == ctx.plan.plan_id
            and receipt.target_adapter == ctx.adapter_id
            and (receipt.target_channel or None) == (ctx.target.channel or None)
        ]
        if not matching:
            return None
        return max(matching, key=lambda receipt: receipt.attempt_number)

    async def _persist_suppression(
        self,
        ctx: _DeliveryContext,
        *,
        failure_kind: DeliveryFailureKind,
        error: str,
    ) -> DeliveryReceipt:
        return await self._persist_suppression_receipt(
            event_id=ctx.event.event_id,
            delivery_plan_id=ctx.plan.plan_id,
            target_adapter=ctx.adapter_id,
            target_channel=ctx.target.channel,
            route_id=ctx.route.id,
            failure_kind=failure_kind,
            error=error,
            source=ctx.source,
            replay_run_id=ctx.replay_run_id,
        )

    def _build_outcome(
        self,
        ctx: _DeliveryContext,
        *,
        status: Literal[
            "success",
            "queued",
            "transient_failure",
            "permanent_failure",
            "skipped",
        ],
        failure_kind: DeliveryFailureKind | None = None,
        receipt: DeliveryReceipt | None = None,
        error: str | None = None,
        failure_kind_detail: str | None = None,
    ) -> DeliveryOutcome:
        return DeliveryOutcome(
            event_id=ctx.event.event_id,
            target_adapter=ctx.adapter_id,
            target_channel=ctx.target.channel,
            route_id=ctx.route.id,
            delivery_plan_id=ctx.plan.plan_id,
            status=status,
            failure_kind=failure_kind,
            receipt=receipt,
            error=error,
            duration_ms=ctx.elapsed_ms(),
            failure_kind_detail=failure_kind_detail,
        )

    def _record_loop_suppressed(self, route_id: str) -> None:
        if self._route_stats is not None:
            self._route_stats.record_loop_prevented(route_id)
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_loop_prevented()

    def _record_capability_suppressed(self, route_id: str) -> None:
        if self._route_stats is not None:
            self._route_stats.record_capability_suppressed(route_id)
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_capability_suppressed()

    def _record_failed(self, route_id: str, error: str) -> None:
        if self._route_stats is not None:
            self._route_stats.record_failed(route_id, error)
        if self._runtime_accounting is not None:
            self._runtime_accounting.record_outbound_failed()

    @staticmethod
    def _inflight_key(ctx: _DeliveryContext) -> str:
        return (
            f"{ctx.event.event_id}:{ctx.route.id}:"
            f"{ctx.adapter_id}:{ctx.plan.plan_id}"
        )
