"""Replay engine: thin orchestration layer for deterministic re-processing.

:class:`ReplayEngine` composes focused stage mixins via MRO:

.. class diagram::

   ReplayEngine
     → _ReplayDeliveryMixin
     → _ReplayRenderingMixin
     → _ReplayPlanningMixin
     → _ReplayRoutingMixin
     → _ReplayStoreMixin
     → _ReplaySelectionMixin
     → _ReplayEngineBase
     → object

Each mixin owns exactly one pipeline stage or selection concern.  This
module holds only the constructor, cancellation state, and the
high-level sequencing loop (:meth:`replay`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from medre.core.engine.replay.delivery import _ReplayDeliveryMixin
from medre.core.engine.replay.helpers import (
    _request_to_filter,
    _resolve_stages,
    _verify_immutability,
)
from medre.core.engine.replay.planning import _ReplayPlanningMixin
from medre.core.engine.replay.protocols import _RealPipelineProtocol
from medre.core.engine.replay.rendering import _ReplayRenderingMixin
from medre.core.engine.replay.routing import _ReplayRoutingMixin
from medre.core.engine.replay.selection import _ReplaySelectionMixin
from medre.core.engine.replay.store import _ReplayStoreMixin
from medre.core.engine.replay.types import ReplayMode, ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent
from medre.core.storage.backend import StorageBackend

if TYPE_CHECKING:
    from medre.core.observability.metrics import Diagnostician
    from medre.core.supervision.accounting import RuntimeAccounting
    from medre.core.supervision.capacity import CapacityController


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Replay engine base (constructor + cancellation)
# ---------------------------------------------------------------------------


class _ReplayEngineBase:
    """Base providing constructor, cancellation state, and capacity wiring."""

    def __init__(
        self,
        storage: StorageBackend,
        pipeline: _RealPipelineProtocol | None = None,
        event_bus: Any | None = None,
        diagnostician: Diagnostician | None = None,
        capacity_controller: CapacityController | None = None,
        accounting: RuntimeAccounting | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._event_bus = event_bus
        self._diagnostician = diagnostician
        self._capacity_controller: CapacityController | None = capacity_controller
        self._accounting: RuntimeAccounting | None = accounting
        self._cancel_event: asyncio.Event = asyncio.Event()

    def set_capacity_controller(self, cc: CapacityController) -> None:
        """Wire a :class:`~medre.core.supervision.capacity.CapacityController`.

        When set, :meth:`_stage_deliver` acquires a replay slot
        before delivery in BEST_EFFORT mode and releases it on
        completion.
        """
        self._capacity_controller = cc

    # -- Cancellation -------------------------------------------------------

    def cancel(self) -> None:
        """Request cancellation of any in-flight replay.

        Idempotent: calling more than once is harmless.  After
        cancellation the engine's ``replay()`` async generator stops
        iterating new events and skips remaining stages for the
        current event, producing ``ReplayResult(status="skipped",
        error="replay_cancelled")`` for each abandoned stage.

        The cancellation signal persists until :meth:`reset_cancellation`
        is called (or a new :class:`ReplayEngine` is constructed).
        """
        self._cancel_event.set()
        _logger.info("Replay cancellation requested")

    @property
    def is_cancelled(self) -> bool:
        """Return ``True`` if cancellation has been requested."""
        return self._cancel_event.is_set()

    def reset_cancellation(self) -> None:
        """Clear the cancellation signal so a new replay can proceed.

        Useful when a :class:`ReplayEngine` instance is reused across
        multiple replay operations (e.g. in long-lived runtimes).
        """
        self._cancel_event.clear()


# ---------------------------------------------------------------------------
# Replay engine (orchestration + mixin composition)
# ---------------------------------------------------------------------------


class ReplayEngine(
    _ReplayDeliveryMixin,
    _ReplayRenderingMixin,
    _ReplayPlanningMixin,
    _ReplayRoutingMixin,
    _ReplayStoreMixin,
    _ReplaySelectionMixin,
    _ReplayEngineBase,
):
    """Replays historical canonical events through selected pipeline stages.

    The replay engine reads events from storage (read-only) and pushes them
    through the specified pipeline stages.  Different :class:`ReplayMode`
    values control which stages are executed and whether side effects
    (delivery to adapters) are allowed.

    Parameters
    ----------
    storage:
        The storage backend to read historical events from.
    pipeline:
        Optional pipeline collaborator that satisfies
        :class:`_RealPipelineProtocol`.  Required for ``RE_RENDER``,
        ``RE_ROUTE``, ``BEST_EFFORT``, and ``DRY_RUN`` modes.
    event_bus:
        Optional event bus for publishing replayed events.  Accepted
        but not currently invoked during replay; reserved for future
        notification use.
    diagnostician:
        Optional :class:`~medre.core.observability.metrics.Diagnostician`
        for recording replay skips, downgrades, renderer failures, and
        adapter failures.  When provided, diagnostic events are emitted
        for each notable replay condition.
    """

    # -- Public API ---------------------------------------------------------

    async def replay(
        self,
        request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Iterate over matching events and replay through requested stages.

        Yields one :class:`ReplayResult` for each ``(event, stage)``
        combination.

        When ``request.correlation_ids`` is set, events are fetched by
        individual ID (via :meth:`StorageBackend.get`) and remaining
        filter criteria are applied as post-filters.  Otherwise a
        standard :meth:`StorageBackend.query` is used.

        **Determinism guarantee:** Results are yielded in the order
        events are returned by storage (timestamp ascending for queries,
        correlation_id list order for ID-based lookups).  For a given
        stored dataset and pipeline configuration, the sequence of
        ``(event_id, stage, status)`` tuples is deterministic.

        **Immutability guarantee:** The replay engine never mutates
        historical :class:`CanonicalEvent` instances.  Events are read
        from storage and passed through pipeline stages without
        modification.  Non-BEST_EFFORT modes produce no storage side
        effects.

        Parameters
        ----------
        request:
            Filter and targeting specification.

        Yields
        ------
        ReplayResult
            Outcome for each stage of each matching event.
        """
        stages = _resolve_stages(request)

        if request.correlation_ids is not None:
            async for event_id, event in self._iter_by_ids(request):
                if self._cancel_event.is_set():
                    _logger.info(
                        "Replay cancelled --- stopping correlation-id iteration",
                    )
                    return
                if event is None:
                    async for result in self._replay_missing(event_id, stages):
                        yield result
                else:
                    async for result in self._replay_event_safe(
                        event,
                        stages,
                        request,
                    ):
                        yield result
        else:
            event_filter = _request_to_filter(request)
            async for event in self._storage.query(event_filter):  # type: ignore[union-attr]
                if self._cancel_event.is_set():
                    _logger.info("Replay cancelled --- stopping event iteration")
                    return
                async for result in self._replay_event_safe(
                    event,
                    stages,
                    request,
                ):
                    yield result

    # -- Internal per-event replay ------------------------------------------

    async def _replay_event_safe(
        self,
        event: CanonicalEvent,
        stages: tuple[str, ...],
        request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event, wrapping with BEST_EFFORT crash-safety.

        Delegates to :meth:`_replay_event` inside a ``try`` block.  In
        :attr:`ReplayMode.BEST_EFFORT` mode any unexpected exception is
        caught and yielded as a single ``"error"`` result so the caller
        is never crashed by an individual event failure.  Other modes
        re-raise the exception.
        """
        mode = request.mode
        try:
            async for result in self._replay_event(event, stages, request):
                yield result
            # All stages completed for this event -> processed.
            if self._accounting is not None:
                self._accounting.record_replay_processed()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if mode is ReplayMode.BEST_EFFORT:
                if self._diagnostician is not None:
                    self._diagnostician.record_adapter_failure(
                        event.event_id,
                        "replay",
                        f"Unexpected error in BEST_EFFORT mode: {exc}",
                    )
                if self._accounting is not None:
                    self._accounting.record_replay_rejected()
                yield ReplayResult(
                    event_id=event.event_id,
                    stage="unknown",
                    status="error",
                    error=f"Unexpected error in BEST_EFFORT mode: {exc}",
                    lineage=list(event.lineage),
                )
            else:
                raise

    async def _replay_event(
        self,
        event: CanonicalEvent,
        stages: tuple[str, ...],
        request: ReplayRequest,
    ) -> AsyncIterator[ReplayResult]:
        """Replay a single event through *stages*, yielding results.

        Carries intermediate state (route results, delivery plans) forward
        between stages so that downstream stages can use upstream outputs.
        Each stage is always attempted; downstream stages gracefully
        handle missing upstream data.
        """
        mode = request.mode
        route_result: list[tuple[Any, Any]] | None = None
        plan_result: list[Any] | None = None
        enriched_event: CanonicalEvent | None = None

        # Immutability guard: checkpoint event identity before processing.
        _verify_immutability(event, event.event_id)

        for stage in stages:
            # Check cancellation between stages --- skip remaining stages
            # for this event if cancellation was requested mid-event.
            if self._cancel_event.is_set():
                yield ReplayResult(
                    event_id=event.event_id,
                    stage=stage,
                    status="skipped",
                    error="replay_cancelled",
                    lineage=list(event.lineage),
                )
                continue
            if stage == "store":
                result = await self._stage_store(event)
            elif stage == "route":
                result, route_result, enriched_event = await self._stage_route(
                    event,
                    request=request,
                )
            elif stage == "plan":
                result, plan_result = await self._stage_plan(
                    enriched_event or event,
                    route_result,
                )
            elif stage == "render":
                result = await self._stage_render(event, mode)
            elif stage == "deliver":
                result = await self._stage_deliver(
                    enriched_event or event,
                    plan_result,
                    request,
                )
            else:
                result = ReplayResult(
                    event_id=event.event_id,
                    stage=stage,
                    status="skipped",
                    error=f"Unknown stage: {stage!r}",
                )
            result.lineage = list(event.lineage)
            yield result
