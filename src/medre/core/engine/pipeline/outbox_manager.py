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
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.delivery_state import (
    TERMINAL_OUTBOX_STATUSES,
)
from medre.core.engine.pipeline.receipt_factory import build_delivery_receipt
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    DeliveryPlan,
    RetryPolicy,
)
from medre.core.routing.models import Route, RouteTarget
from medre.core.storage.backend import DeliveryOutboxItem, StorageBackend

# -- Constants --

_OUTBOX_RENEWAL_INTERVAL_SECONDS: int = 30  # seconds between lease renewals
_OUTBOX_RENEWAL_DURATION_SECONDS: int = 60  # lease TTL (kept short; renewed)


@dataclass(frozen=True)
class OutboxContext:
    """Result of outbox item creation for a delivery attempt."""

    outbox_id: str | None
    created: bool
    pipeline_worker: str
    skip_reason: str | None


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
        the method queries existing outbox rows for the same event and
        computes ``max(attempt_number) + 1`` across rows sharing the same
        delivery identity (delivery_plan_id, target_adapter, target_channel).
        This guarantees replay never reclaims or mutates live rows (which
        have lower attempt numbers).  The same ownership check applies to
        ALL sources — if the freshly-created replay row comes back terminal,
        active, or owned by another worker, delivery is skipped.
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

            attempt_number = 1

            # For replay: compute attempt_number = max(existing) + 1 so
            # the new row cannot conflict with any live or prior replay row.
            if source == "replay":
                existing = await self._storage.list_outbox_items_for_event(
                    event.event_id,
                )
                for row in existing:
                    if (
                        row.delivery_plan_id == route_plan.plan_id
                        and row.target_adapter == adapter_name
                        and (row.target_channel or None) == (target.channel or None)
                    ):
                        attempt_number = max(attempt_number, row.attempt_number + 1)

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
                metadata=_dest_meta,
            )
            created = await self._storage.create_outbox_item(outbox_item)
            outbox_id = created.outbox_id
            outbox_created = True

            # Ownership check — must run BEFORE we update pipeline_worker
            # so that we compare the persisted worker_id against the
            # pipeline's own worker_id (not the overridden value).
            # Applies to ALL sources including replay.
            skip_reason: str | None = None
            if created.status in TERMINAL_OUTBOX_STATUSES:
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
            )
        except Exception:
            self._log.exception(
                "Failed to create outbox item for event_id=%s adapter=%s",
                event.event_id,
                adapter_name,
            )
            # Non-fatal: pipeline continues without outbox tracking.
            skip_reason = "outbox_creation_failed"
        return OutboxContext(
            outbox_id=outbox_id,
            created=outbox_created,
            pipeline_worker=pipeline_worker,
            skip_reason=skip_reason,
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
        receipt: DeliveryReceipt | None,
        failure_kind_val: DeliveryFailureKind | None,
        error: str | None,
        retry_policy: RetryPolicy | None,
    ) -> None:
        """Update the outbox item status based on the delivery outcome.

        Thin wrapper that delegates to
        :class:`~medre.core.engine.pipeline.delivery_lifecycle.DeliveryLifecycleService`.

        See :meth:`DeliveryLifecycleService.finalize_outbox_outcome`
        for full documentation.
        """
        await self._lifecycle.finalize_outbox_outcome(
            self._storage,
            outbox_id=ctx.outbox_id,
            outbox_created=ctx.created,
            receipt=receipt,
            failure_kind_val=failure_kind_val,
            error=error,
            retry_policy=retry_policy,
        )

    # -- Terminal outcome recording --

    async def record_terminal(self, record: QueueTerminalRecord) -> None:
        """Handle a terminal queue outcome reported by an adapter.

        Called by queue-based adapters when a previously-enqueued item
        reaches a terminal state without producing a native message ID.
        This is the callback wired into
        :class:`AdapterContext.record_outbound_terminal`.

        Creates a durable receipt recording the terminal outcome and
        transitions the matching outbox item to the appropriate terminal
        status.  Adapters report facts; this method (core/pipeline)
        decides the lifecycle authority mapping.

        Parameters
        ----------
        record:
            The terminal outcome record from the adapter.
        """
        try:
            # Map adapter-reported outcome to receipt status and outbox
            # terminal status.
            if record.outcome == "exhausted":
                receipt_status = "failed"
                failure_kind = "adapter_transient"
                outbox_terminal = "dead_lettered"
                error_msg = record.error or "local queue retry budget exhausted"
            elif record.outcome == "permanent_failed":
                receipt_status = "failed"
                failure_kind = "adapter_permanent"
                outbox_terminal = "dead_lettered"
                error_msg = record.error or "permanent send failure"
            elif record.outcome == "cancelled":
                receipt_status = "failed"
                failure_kind = "adapter_transient"
                outbox_terminal = "cancelled"
                error_msg = record.error or "queue item cancelled while in-flight"
            elif record.outcome == "abandoned":
                receipt_status = "failed"
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

            # Pre-validate outbox item state before creating the receipt.
            # Reject terminal reports for outbox items that are already in
            # a terminal state or that do not exist, to prevent duplicate
            # terminal receipts and outbox-transition errors.
            # Track the validated item for attempt_number and field enrichment.
            existing_item: DeliveryOutboxItem | None = None
            if record.outbox_id is not None:
                existing_item = await self._storage.get_outbox_item(
                    record.outbox_id,
                )
                if existing_item is None:
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s not found "
                        "for event_id=%s adapter=%s outcome=%s; "
                        "outbox item may have been garbage-collected",
                        record.outbox_id,
                        record.event_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                if existing_item.status in TERMINAL_OUTBOX_STATUSES:
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s already "
                        "terminal (status=%s) for event_id=%s adapter=%s "
                        "outcome=%s; duplicate terminal report",
                        record.outbox_id,
                        existing_item.status,
                        record.event_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Validate event_id matches to prevent cross-event corruption.
                if existing_item.event_id != record.event_id:
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "event_id=%s but record has event_id=%s; "
                        "adapter=%s outcome=%s",
                        record.outbox_id,
                        existing_item.event_id,
                        record.event_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Validate adapter matches the outbox row.
                if existing_item.target_adapter != record.adapter:
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "target_adapter=%s but record has adapter=%s; "
                        "outcome=%s",
                        record.outbox_id,
                        existing_item.target_adapter,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Validate channel matches the outbox row.
                if record.native_channel_id is not None and (
                    record.native_channel_id or None
                ) != (existing_item.target_channel or None):
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "target_channel=%s but record has native_channel_id=%s; "
                        "adapter=%s outcome=%s",
                        record.outbox_id,
                        existing_item.target_channel,
                        record.native_channel_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Validate delivery_plan_id matches the outbox row.
                if (
                    record.delivery_plan_id is not None
                    and record.delivery_plan_id != existing_item.delivery_plan_id
                ):
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "delivery_plan_id=%s but record has %s; "
                        "adapter=%s outcome=%s",
                        record.outbox_id,
                        existing_item.delivery_plan_id,
                        record.delivery_plan_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Validate attempt_number — required for queue callbacks.
                if record.attempt_number is None:
                    self._log.warning(
                        "Terminal outcome rejected: missing attempt_number "
                        "for outbox_id=%s adapter=%s outcome=%s; "
                        "queue terminal callbacks must carry attempt_number",
                        record.outbox_id,
                        record.adapter,
                        record.outcome,
                    )
                    return
                if record.attempt_number != existing_item.attempt_number:
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "attempt_number=%d but record has %d; "
                        "adapter=%s outcome=%s",
                        record.outbox_id,
                        existing_item.attempt_number,
                        record.attempt_number,
                        record.adapter,
                        record.outcome,
                    )
                    return
                # Only queued/in_progress are eligible for terminal outcome
                # recording — pending and retry_wait indicate states where
                # the adapter should not be reporting a terminal outcome.
                if existing_item.status not in ("queued", "in_progress"):
                    self._log.warning(
                        "Terminal outcome rejected: outbox_id=%s has "
                        "status=%s which is not eligible for queue terminal "
                        "outcomes (expected queued or in_progress); "
                        "adapter=%s outcome=%s",
                        record.outbox_id,
                        existing_item.status,
                        record.adapter,
                        record.outcome,
                    )
                    return
            else:
                # No outbox_id — hard-reject.  Queue terminal callbacks
                # MUST carry outbox_id for exact correlation.
                self._log.warning(
                    "Terminal outcome rejected: no outbox_id for "
                    "event_id=%s adapter=%s outcome=%s; exact outbox "
                    "correlation is required",
                    record.event_id,
                    record.adapter,
                    record.outcome,
                )
                return

            # Derive attempt_number: prefer the validated outbox item's value
            # (authoritative), then the record's, then default to 1.
            _attempt_number: int = 1
            if existing_item is not None:
                _attempt_number = existing_item.attempt_number
            elif record.attempt_number is not None:
                _attempt_number = record.attempt_number

            # Enrich receipt fields from the validated outbox item when
            # available — the outbox row is the authoritative source for
            # delivery_plan_id, target_channel, and route_id.
            _enriched_plan_id = (
                existing_item.delivery_plan_id
                if existing_item is not None
                else (record.delivery_plan_id or "")
            )
            _enriched_channel = (
                existing_item.target_channel
                if existing_item is not None
                else record.native_channel_id
            )

            # Create the terminal receipt.
            receipt = build_delivery_receipt(
                event_id=record.event_id,
                delivery_plan_id=_enriched_plan_id,
                target_adapter=record.adapter,
                target_channel=_enriched_channel,
                route_id=(existing_item.route_id if existing_item is not None else ""),
                status=receipt_status,
                error=error_msg,
                failure_kind=failure_kind,
                source="live",
                outbox_id=record.outbox_id,
                attempt_number=_attempt_number,
            )
            await self._storage.append_receipt(receipt)

            # Transition the outbox item to terminal status.
            if record.outbox_id is not None:
                try:
                    if outbox_terminal == "dead_lettered":
                        await self._storage.mark_outbox_dead_lettered(
                            record.outbox_id,
                            receipt_id=receipt.receipt_id,
                            failure_kind=failure_kind,
                            error_summary=error_msg[:200] if error_msg else None,
                        )
                    elif outbox_terminal == "cancelled":
                        await self._storage.mark_outbox_cancelled(
                            record.outbox_id,
                            error_summary=error_msg[:200] if error_msg else None,
                        )
                    elif outbox_terminal == "abandoned":
                        await self._storage.mark_outbox_abandoned(
                            record.outbox_id,
                            error_summary=error_msg[:200] if error_msg else None,
                        )
                except Exception:
                    self._log.exception(
                        "Failed to transition outbox to %s: outbox_id=%s "
                        "event_id=%s adapter=%s",
                        outbox_terminal,
                        record.outbox_id,
                        record.event_id,
                        record.adapter,
                    )

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
