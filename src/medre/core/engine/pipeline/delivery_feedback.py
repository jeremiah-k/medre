"""Unified adapter delivery-feedback dispatch.

Adapters report asynchronous transport facts through one closed feedback union.
This dispatcher demultiplexes those facts into existing lifecycle authorities;
it deliberately contains no receipt/outbox policy of its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from medre.core.contracts.delivery import (
    DeferredHandoffCompleted,
    DeferredHandoffFailed,
    DeliveryFeedback,
    PostHandoffObservation,
)
from medre.core.engine.pipeline.delivery_lifecycle import DeliveryLifecycleService
from medre.core.engine.pipeline.outbox_manager import OutboxManager
from medre.core.storage.backend import StorageBackend


class DeliveryFeedbackDispatcher:
    """Route one adapter feedback union into core lifecycle authority.

    The dispatcher is intentionally thin. It owns callback containment and
    routing only; :class:`DeliveryLifecycleService` and :class:`OutboxManager`
    continue to own validation and durable state transitions.
    """

    def __init__(
        self,
        *,
        storage: StorageBackend,
        lifecycle: DeliveryLifecycleService,
        outbox_manager: OutboxManager,
        native_ref_persisted_fn: Callable[[str], Awaitable[None]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._lifecycle = lifecycle
        self._outbox_manager = outbox_manager
        self._native_ref_persisted_fn = native_ref_persisted_fn
        self._log = logger or logging.getLogger(__name__)

    async def record(self, feedback: DeliveryFeedback) -> None:
        """Record one asynchronous adapter fact without leaking failures back.

        Built-in adapters call this from queue/session background work. Stale or
        contradictory feedback is expected to fail closed inside lifecycle
        authority. Unexpected storage failures are logged and contained so a
        reporting failure cannot terminate the adapter's transport worker.
        """
        if isinstance(feedback, DeferredHandoffCompleted):
            await self._record_deferred_completion(feedback)
            return
        if isinstance(feedback, DeferredHandoffFailed):
            await self._outbox_manager.record_deferred_failure(feedback)
            return
        if isinstance(feedback, PostHandoffObservation):
            await self._record_observation(feedback)
            return
        raise TypeError(
            f"unsupported delivery feedback type: {type(feedback).__name__}"
        )

    async def _record_deferred_completion(
        self,
        feedback: DeferredHandoffCompleted,
    ) -> None:
        provenance = feedback.attempt_provenance
        try:
            committed = await self._lifecycle.finalize_deferred_handoff(
                self._storage,
                feedback,
                datetime.now(tz=timezone.utc),
            )
            if (
                committed
                and feedback.handoff.native_message_id is not None
                and self._native_ref_persisted_fn is not None
            ):
                await self._native_ref_persisted_fn(provenance.event_id)
        except Exception:
            self._log.exception(
                "Failed to finalize deferred adapter hand-off: "
                "event_id=%s adapter=%s outbox_id=%s attempt=%d",
                provenance.event_id,
                provenance.target_adapter,
                provenance.outbox_id,
                provenance.attempt_number,
            )

    async def _record_observation(self, feedback: PostHandoffObservation) -> None:
        provenance = feedback.attempt_provenance
        # Post-handoff observations are fire-once side evidence. Preserve the
        # previous one-yield retry for transient executor/write contention.
        for attempt in (1, 2):
            try:
                await self._lifecycle.record_post_handoff_observation(
                    self._storage,
                    feedback,
                    datetime.now(tz=timezone.utc),
                )
                return
            except Exception:
                if attempt == 2:
                    self._log.exception(
                        "Failed to persist post-handoff observation: "
                        "event_id=%s adapter=%s state=%s outbox_id=%s attempt=%d",
                        provenance.event_id,
                        provenance.target_adapter,
                        feedback.state,
                        provenance.outbox_id,
                        provenance.attempt_number,
                    )
                else:
                    await asyncio.sleep(0)
