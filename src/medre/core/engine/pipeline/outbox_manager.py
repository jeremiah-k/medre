"""Outbox lifecycle management extracted from PipelineRunner.

Centralizes outbox creation, lease renewal, outcome finalization, and
terminal outcome recording. PipelineRunner delegates to this module for
all outbox state transitions.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from medre.core.contracts.adapter import QueueTerminalRecord
from medre.core.delivery_authority import (
    delivery_attempt_provenance_mismatch,
    delivery_attempt_receipt_provenance_mismatch,
    receipts_for_attempt,
    queued_receipts_for_attempt,
)
from medre.core.engine.pipeline.delivery_evidence import DeliveryExecutionEvidence
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.delivery_state import (
    TERMINAL_OUTBOX_STATUSES,
)
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events import (
    normalize_delivery_provenance,
)
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryPlan,
    RetryPolicy,
)
from medre.core.routing.models import Route, RouteTarget
from medre.core.storage.backend import (
    DeliveryOutboxItem,
    StorageBackend,
    TerminalOutboxFinalization,
)

# -- Constants --

_OUTBOX_RENEWAL_INTERVAL_SECONDS: int = 30  # seconds between lease renewals
_OUTBOX_RENEWAL_DURATION_SECONDS: int = 60  # lease TTL (kept short; renewed)
OUTBOX_CREATION_FAILED_REASON: str = "outbox_creation_failed"


@dataclass(frozen=True)
class OutboxContext:
    """Result of outbox item creation for a delivery attempt."""

    outbox_id: str | None
    created: bool
    pipeline_worker: str
    skip_reason: str | None
    attempt_number: int | None = None
    replay_duplicate: bool = False


class OutboxManager:
    """Manages the outbox lifecycle for delivery attempts.

    Extracted from PipelineRunner to centralize outbox creation,
    lease renewal, outcome finalization, and terminal outcome recording.
    """

    def __init__(
        self,
        storage: StorageBackend,
        lifecycle: DeliveryLifecycleService,
    ) -> None:
        self._storage = storage
        self._lifecycle = lifecycle
        self._log = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    # -- Creation --

    async def create_for_delivery(
        self,
        event: CanonicalEvent,
        route: Route,
        route_plan: DeliveryPlan,
        target: RouteTarget,
        adapter_name: str,
        *,
        source: str = "live",
        replay_run_id: str | None = None,
    ) -> OutboxContext:
        """Create a durable outbox item tracking a delivery attempt.

        Returns an :class:`OutboxContext` with the outbox ID, creation
        flag, worker identity, and optional skip reason.

        *skip_reason* is ``None`` when the pipeline owns the row (status
        ``"in_progress"`` with matching worker_id).  Otherwise it is set to
        a descriptive string:

        * ``"terminal:<status>"`` — row is in a terminal state.
        * ``"active:queued"`` — row is queued (owned by another worker).
        * ``"active:other_worker:<id>"`` — row is in_progress but owned by
          another worker.

        **Replay attempt identity rule.**  When *source* is ``"replay"``,
        storage atomically allocates and inserts one fresh outbox generation at
        ``max(effective_attempt) + 1`` for the event-scoped delivery identity
        (event_id, delivery_plan_id, target_adapter, target_channel).
        ``effective_attempt`` means a live ``active_attempt`` reservation when
        present, otherwise the finalized ``attempt_number``.  A non-empty
        *replay_run_id* additionally claims that logical target atomically: a
        second execution of the same run reuses the existing row instead of
        allocating a sibling generation.  The run ID is durable execution
        provenance/idempotency metadata and is deliberately not part of
        ``DeliveryIdentity``.  Empty run IDs remain repeatable.
        """
        source, replay_run_id = normalize_delivery_provenance(source, replay_run_id)
        outbox_id: str | None = None
        outbox_created: bool = False
        pipeline_worker: str = ""
        try:
            _now = datetime.now(timezone.utc)
            pipeline_worker = f"pipeline:{uuid.uuid4().hex[:12]}"
            _lease_until = (
                _now + timedelta(seconds=_OUTBOX_RENEWAL_DURATION_SECONDS)
            ).isoformat()
            _dest_meta: dict | None = None
            if target.destination is not None:
                _dest_meta = {
                    "destination_kind": target.destination.kind,
                    "destination_hash": target.destination.destination_hash,
                    "destination_name": target.destination.destination_name,
                    "destination_metadata": target.destination.metadata,
                }

            # Persist route-decision metadata so retry reconstruction
            # recovers the original capability and strategy decisions
            # instead of defaulting to capability_level=None / strategy="direct".
            _route_decision_meta: dict[str, object] = {
                "capability_level": route_plan.capability_level,
                "delivery_strategy": route_plan.primary_strategy.method,
                "capability_field": route_plan.capability_field,
                "capability_reason": route_plan.capability_reason,
                "deadline": (
                    route_plan.deadline.isoformat()
                    if route_plan.deadline is not None
                    else None
                ),
            }
            if _dest_meta is not None:
                _dest_meta.update(_route_decision_meta)
            else:
                _dest_meta = _route_decision_meta

            # Replay requires a fresh durable generation. The storage backend
            # allocates it atomically with insertion; the placeholder value is
            # ignored in replay mode. Live delivery retains normal idempotent
            # attempt-1 creation/reclaim semantics.
            attempt_number = 1

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
                attempt_number=attempt_number,
                status="in_progress",
                locked_at=_now.isoformat(),
                lease_until=_lease_until,
                worker_id=pipeline_worker,
                dispatch_source=source,
                replay_run_id=(
                    replay_run_id if source == "replay" and replay_run_id else None
                ),
                metadata=_dest_meta,
            )
            created = await self._storage.create_outbox_item(
                outbox_item,
                allocate_new_generation=(source == "replay"),
            )
            outbox_id = created.outbox_id
            outbox_created = True
            replay_duplicate = bool(
                source == "replay"
                and replay_run_id
                and created.outbox_id != outbox_item.outbox_id
                and created.replay_run_id == replay_run_id
            )

            # Ownership check — must run BEFORE we update pipeline_worker
            # so that we compare the persisted worker_id against the
            # pipeline's own worker_id (not the overridden value).
            # Applies to ALL sources including replay.
            skip_reason: str | None = None
            if replay_duplicate:
                skip_reason = f"replay_run_claimed:{created.status}"
                self._log.info(
                    "replay_run_skip: event_id=%s adapter=%s outbox_id=%s run_id=%s status=%s",
                    event.event_id,
                    adapter_name,
                    created.outbox_id,
                    replay_run_id,
                    created.status,
                )
            elif created.status in TERMINAL_OUTBOX_STATUSES:
                skip_reason = f"terminal:{created.status}"
                self._log.info(
                    "outbox_skip: event_id=%s adapter=%s outbox_id=%s status=%s (terminal, not delivering)",
                    event.event_id,
                    adapter_name,
                    created.outbox_id,
                    created.status,
                )
            elif created.status == "queued":
                skip_reason = "active:queued"
                self._log.info(
                    "outbox_skip: event_id=%s adapter=%s outbox_id=%s status=queued (active, not stealing)",
                    event.event_id,
                    adapter_name,
                    created.outbox_id,
                )
            elif (
                created.status == "in_progress" and created.worker_id != pipeline_worker
            ):
                owner_id = created.worker_id or "unknown"
                skip_reason = f"active:other_worker:{owner_id}"
                self._log.info(
                    "outbox_skip: event_id=%s adapter=%s outbox_id=%s owner=%s (active, not stealing)",
                    event.event_id,
                    adapter_name,
                    created.outbox_id,
                    created.worker_id,
                )

            # create_outbox_item may return an existing non-terminal row;
            # always use the persisted owner for lease renewals.
            pipeline_worker = created.worker_id or pipeline_worker

            return OutboxContext(
                outbox_id=outbox_id,
                created=outbox_created,
                pipeline_worker=pipeline_worker,
                skip_reason=skip_reason,
                attempt_number=self._lifecycle.effective_attempt(created),
                replay_duplicate=replay_duplicate,
            )
        except Exception:
            self._log.exception(
                "Failed to create outbox item for event_id=%s adapter=%s",
                event.event_id,
                adapter_name,
            )
            # Non-fatal: pipeline continues without outbox tracking.
            skip_reason = OUTBOX_CREATION_FAILED_REASON
        return OutboxContext(
            outbox_id=outbox_id,
            created=outbox_created,
            pipeline_worker=pipeline_worker,
            skip_reason=skip_reason,
            attempt_number=None,
            replay_duplicate=False,
        )

    # -- Lease renewal --

    def start_lease_renewal(
        self,
        ctx: OutboxContext,
    ) -> asyncio.Task | None:
        """Start a background task that periodically renews the outbox lease.

        Returns the :class:`asyncio.Task` managing the renewal loop, or
        ``None`` if no outbox item was created.
        """
        outbox_id = ctx.outbox_id
        outbox_created = ctx.created
        pipeline_worker = ctx.pipeline_worker

        async def _renew_lease() -> None:
            while True:
                await asyncio.sleep(_OUTBOX_RENEWAL_INTERVAL_SECONDS)
                if outbox_id is not None:
                    try:
                        _new_lease = (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=_OUTBOX_RENEWAL_DURATION_SECONDS)
                        ).isoformat()
                        renewed = await self._storage.renew_outbox_lease(
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

    @staticmethod
    async def cancel_renewal(task: asyncio.Task | None) -> None:
        """Cancel a lease renewal task cleanly."""
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.getLogger(__name__).debug(
                    "Outbox lease renewal task ended with error",
                    exc_info=True,
                )

    # -- Outcome finalization --

    async def finalize_outcome(
        self,
        ctx: OutboxContext,
        evidence: DeliveryExecutionEvidence,
        retry_policy: RetryPolicy | None,
    ) -> bool | None:
        """Update the outbox item status based on the delivery outcome.

        Thin wrapper that delegates to
        :class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.

        See :meth:`DeliveryLifecycleService.finalize_outbox_outcome`
        for full documentation.
        """
        return await self._lifecycle.finalize_outbox_outcome(
            self._storage,
            outbox_id=ctx.outbox_id,
            outbox_created=ctx.created,
            evidence=evidence,
            retry_policy=retry_policy,
            reserved_attempt_number=ctx.attempt_number,
            expected_worker_id=ctx.pipeline_worker or None,
        )

    # -- Terminal outcome recording --

    async def record_terminal(self, record: QueueTerminalRecord) -> None:
        """Handle a terminal queue outcome reported by an adapter.

        Called by queue-based adapters when a previously-enqueued item
        reaches a terminal state without producing a native message ID.
        This is the callback wired into
        :class:`AdapterContext.record_outbound_terminal`.

        For an eligible callback, commits a terminal lifecycle receipt and
        matching outbox transition atomically. A dead-lettered outcome also
        commits failed-attempt evidence in that transaction. Stale or
        duplicate callbacks commit nothing. Invalid callbacks and storage
        errors return without raising to the adapter.

        Parameters
        ----------
        record:
            The terminal outcome record from the adapter.
        """
        try:
            # Map adapter-reported facts to one lifecycle transition. Queue
            # callbacks do not represent a new dispatch attempt; the queued
            # receipt is the attempt evidence and this receipt records only the
            # terminal state transition caused by it.
            if record.outcome == "exhausted":
                failure_kind = "adapter_transient"
                outbox_terminal = "dead_lettered"
                error_msg = record.error or "local queue retry budget exhausted"
            elif record.outcome == "permanent_failed":
                failure_kind = "adapter_permanent"
                outbox_terminal = "dead_lettered"
                error_msg = record.error or "permanent send failure"
            elif record.outcome == "cancelled":
                failure_kind = "adapter_transient"
                outbox_terminal = "cancelled"
                error_msg = record.error or "queue item cancelled while in-flight"
            elif record.outcome == "abandoned":
                failure_kind = "adapter_transient"
                outbox_terminal = "abandoned"
                error_msg = record.error or "adapter shutdown with unsent queued items"
            else:
                self._log.warning(
                    "Unknown terminal outcome %r from adapter %s; ignoring",
                    record.outcome,
                    record.adapter,
                )
                return

            # Exact attempt provenance is mandatory for queue terminal callbacks.
            # It was frozen before adapter hand-off, so callback lineage never
            # depends on mutable row state or queued-receipt append timing.
            provenance = record.attempt_provenance
            if provenance is None:
                self._log.warning(
                    "Terminal outcome rejected: missing attempt_provenance for "
                    "event_id=%s adapter=%s outcome=%s",
                    record.event_id,
                    record.adapter,
                    record.outcome,
                )
                return

            existing_item = await self._storage.get_outbox_item(provenance.outbox_id)
            if existing_item is None:
                self._log.warning(
                    "Terminal outcome rejected: outbox_id=%s not found for "
                    "event_id=%s adapter=%s outcome=%s",
                    provenance.outbox_id,
                    record.event_id,
                    record.adapter,
                    record.outcome,
                )
                return
            if existing_item.status in TERMINAL_OUTBOX_STATUSES:
                self._log.warning(
                    "Terminal outcome rejected: outbox_id=%s already terminal "
                    "(status=%s); duplicate terminal report",
                    provenance.outbox_id,
                    existing_item.status,
                )
                return

            mismatch = delivery_attempt_provenance_mismatch(provenance, existing_item)
            if mismatch is not None:
                self._log.warning(
                    "Terminal outcome rejected: contradictory attempt provenance "
                    "for outbox_id=%s: %s",
                    provenance.outbox_id,
                    mismatch,
                )
                return

            # ``native_channel_id`` is transport-reported evidence rather than
            # a provenance mirror, but queue terminal callbacks historically
            # required it to agree with the admitted target when supplied. Do
            # not let the envelope migration weaken that independent fence.
            if record.native_channel_id is not None and (
                record.native_channel_id or None
            ) != (existing_item.target_channel or None):
                self._log.warning(
                    "Terminal outcome rejected: native_channel_id mismatch for "
                    "outbox_id=%s callback=%r row=%r",
                    provenance.outbox_id,
                    record.native_channel_id,
                    existing_item.target_channel,
                )
                return

            if existing_item.status not in ("queued", "in_progress"):
                self._log.warning(
                    "Terminal outcome rejected: outbox_id=%s has status=%s which "
                    "is not eligible for queue terminal outcomes",
                    provenance.outbox_id,
                    existing_item.status,
                )
                return

            _attempt_number = provenance.attempt_number

            # Read all immutable evidence for this outbox generation for
            # provenance validation. Queued evidence is then used only for
            # parent/retry linkage. A missing receipt is a valid pre-append race
            # and no longer causes source/replay provenance to be reconstructed.
            queued_receipt: DeliveryReceipt | None = None
            try:
                _all_receipts = await self._storage.list_receipts_for_outbox(
                    provenance.outbox_id,
                )
                _attempt_receipts = receipts_for_attempt(
                    provenance,
                    _all_receipts,
                )
                _queued_matches = queued_receipts_for_attempt(
                    provenance,
                    _all_receipts,
                )
            except Exception:
                self._log.warning(
                    "Could not read attempt receipt lineage for outbox_id=%s; "
                    "rejecting terminal callback rather than committing "
                    "incomplete provenance/linkage validation",
                    provenance.outbox_id,
                )
                return

            for attempt_receipt in _attempt_receipts:
                receipt_mismatch = delivery_attempt_receipt_provenance_mismatch(
                    provenance,
                    attempt_receipt,
                )
                if receipt_mismatch is not None:
                    self._log.warning(
                        "Terminal outcome rejected: immutable receipt provenance "
                        "contradicts callback for outbox_id=%s attempt=%d: %s",
                        provenance.outbox_id,
                        _attempt_number,
                        receipt_mismatch,
                    )
                    return
            if _queued_matches:
                queued_receipt = max(
                    _queued_matches,
                    key=lambda receipt: (
                        receipt.sequence or 0,
                        receipt.created_at.isoformat(),
                        receipt.receipt_id,
                    ),
                )

            # Enrich receipt fields from the validated outbox item when
            # available — the outbox row is the authoritative source for
            # delivery_plan_id, target_channel, and route_id.
            _enriched_plan_id = existing_item.delivery_plan_id
            _enriched_channel = existing_item.target_channel

            # Build evidence for the queue terminal fact. A queue failure
            # proves that the already-enqueued dispatch attempt failed, so it
            # receives a failed attempt receipt. Cancellation/abandonment are
            # lifecycle-only transitions and do not manufacture a failure.
            failed_attempt: DeliveryReceipt | None = None
            lifecycle_parent_id = (
                queued_receipt.receipt_id if queued_receipt is not None else None
            )
            if outbox_terminal == "dead_lettered":
                failed_attempt = build_delivery_receipt(
                    event_id=record.event_id,
                    delivery_plan_id=_enriched_plan_id,
                    target_adapter=record.adapter,
                    target_channel=_enriched_channel,
                    route_id=existing_item.route_id,
                    status="failed",
                    receipt_kind="attempt",
                    error=error_msg,
                    failure_kind=failure_kind,
                    source=provenance.source,
                    replay_run_id=provenance.replay_run_id,
                    parent_receipt_id=lifecycle_parent_id,
                    retry_max_attempts=(
                        queued_receipt.retry_max_attempts
                        if queued_receipt is not None
                        else None
                    ),
                    retry_backoff_base=(
                        queued_receipt.retry_backoff_base
                        if queued_receipt is not None
                        else None
                    ),
                    retry_max_delay=(
                        queued_receipt.retry_max_delay
                        if queued_receipt is not None
                        else None
                    ),
                    retry_jitter=(
                        queued_receipt.retry_jitter
                        if queued_receipt is not None
                        else None
                    ),
                    outbox_id=record.outbox_id,
                    attempt_number=_attempt_number,
                )
                lifecycle_parent_id = failed_attempt.receipt_id

            # Reuse the lifecycle constructor whenever immutable attempt
            # evidence exists so retry-policy lineage is inherited exactly as
            # it is for synchronous failures. Rendering evidence remains on the
            # queued parent by design; the parent chain preserves that link.
            terminal_parent = failed_attempt or queued_receipt
            if terminal_parent is not None:
                receipt = self._lifecycle.build_terminal_lifecycle_receipt(
                    terminal_parent,
                    status=outbox_terminal,
                    error=error_msg,
                    failure_kind=failure_kind,
                )
            else:
                # Callback-before-receipt is valid. The immutable envelope is
                # sufficient for identity/source authority, but unavailable
                # queued-only retry/rendering context must not be invented.
                receipt = build_delivery_receipt(
                    event_id=record.event_id,
                    delivery_plan_id=_enriched_plan_id,
                    target_adapter=record.adapter,
                    target_channel=_enriched_channel,
                    route_id=existing_item.route_id,
                    status=outbox_terminal,
                    receipt_kind="lifecycle",
                    error=error_msg,
                    failure_kind=failure_kind,
                    source=provenance.source,
                    replay_run_id=provenance.replay_run_id,
                    parent_receipt_id=lifecycle_parent_id,
                    outbox_id=record.outbox_id,
                    attempt_number=_attempt_number,
                )
            # Commit any newly-proven failed-attempt evidence, terminal
            # lifecycle evidence, and the outbox transition in one guarded
            # transaction. A stale/duplicate callback therefore commits none
            # of them.
            committed = await self._storage.finalize_outbox_terminal(
                TerminalOutboxFinalization(
                    lifecycle_receipt=receipt,
                    attempt_receipt=failed_attempt,
                )
            )
            if not committed:
                self._log.warning(
                    "Terminal outcome rejected: outbox_id=%s was finalized "
                    "by a competing attempt or state change before commit; "
                    "event_id=%s adapter=%s outcome=%s; duplicate prevented",
                    record.outbox_id,
                    record.event_id,
                    record.adapter,
                    record.outcome,
                )
                return

            self._log.info(
                "Terminal queue outcome: event_id=%s adapter=%s "
                "outbox_id=%s outcome=%s receipt=%s",
                record.event_id,
                record.adapter,
                record.outbox_id,
                record.outcome,
                receipt.receipt_id,
            )
        except Exception:
            self._log.exception(
                "Failed to record terminal queue outcome: "
                "event_id=%s adapter=%s outcome=%s",
                record.event_id,
                record.adapter,
                record.outcome,
            )
