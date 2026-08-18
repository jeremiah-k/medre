"""Recoverable worker for routing durably admitted inbound events."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Protocol

from medre.core.ingress.types import IngressWorkItem

_logger = logging.getLogger(__name__)


class _IngressStorage(Protocol):
    async def claim_ingress_work(
        self, *, worker_id: str, limit: int, lease_seconds: float
    ) -> list[IngressWorkItem]: ...

    async def complete_ingress_work(self, event_id: str, *, worker_id: str) -> None: ...

    async def release_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> None: ...


class _IngressPipeline(Protocol):
    async def process_admitted_event(self, event_id: str) -> object: ...


class DurableIngressWorker:
    """Claim and process persisted ingress work until stopped.

    A processing exception returns the row to ``pending`` with a bounded error
    summary. Cancellation leaves the lease in place; the row becomes claimable
    after lease expiry on the next worker generation.
    """

    def __init__(
        self,
        *,
        storage: _IngressStorage,
        pipeline: _IngressPipeline,
        interval_seconds: float = 0.5,
        batch_size: int = 25,
        lease_seconds: float = 30.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._storage = storage
        self._pipeline = pipeline
        self._interval_seconds = interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._logger = logger or _logger
        self._worker_id = f"ingress-{uuid.uuid4().hex}"
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._processed = 0
        self._failures = 0

    @property
    def running(self) -> bool:
        """Return whether the worker task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def processed(self) -> int:
        """Return successfully completed work count for this generation."""
        return self._processed

    @property
    def failures(self) -> int:
        """Return processing failures observed by this generation."""
        return self._failures

    async def start(self) -> None:
        """Start the background claim loop idempotently."""
        if self.running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="medre-durable-ingress")

    async def stop(self) -> None:
        """Stop claiming new work and cancel any active polling wait."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(self) -> int:
        """Claim one batch and process it, returning completed count."""
        work = await self._storage.claim_ingress_work(
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        completed = 0
        for item in work:
            try:
                await self._pipeline.process_admitted_event(item.event_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                self._logger.exception(
                    "Durable ingress processing failed: event_id=%s attempt=%d",
                    item.event_id,
                    item.attempts,
                )
                await self._storage.release_ingress_work(
                    item.event_id,
                    worker_id=self._worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                await self._storage.complete_ingress_work(
                    item.event_id, worker_id=self._worker_id
                )
                completed += 1
                self._processed += 1
        return completed

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                processed = 0
                self._logger.exception(
                    "Durable ingress claim cycle failed; retrying"
                )
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._interval_seconds
                    )
                except TimeoutError:
                    pass
