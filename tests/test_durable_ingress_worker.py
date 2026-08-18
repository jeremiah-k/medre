"""Durable ingress worker ownership and retry tests."""

from __future__ import annotations

from dataclasses import replace

from medre.core.ingress.types import IngressWorkItem
from medre.core.ingress.worker import DurableIngressWorker


def _work(event_id: str = "evt-1", attempts: int = 1) -> IngressWorkItem:
    return IngressWorkItem(
        event_id=event_id,
        provenance="live",
        status="processing",
        attempts=attempts,
        last_error=None,
        created_at="2026-08-18T00:00:00+00:00",
        updated_at="2026-08-18T00:00:00+00:00",
        worker_id="claimed",
    )


class _Storage:
    def __init__(self, work: list[IngressWorkItem]) -> None:
        self.work = work
        self.completed: list[str] = []
        self.released: list[tuple[str, str]] = []

    async def claim_ingress_work(self, **_kwargs):
        work, self.work = self.work, []
        return work

    async def complete_ingress_work(self, event_id: str, *, worker_id: str) -> None:
        self.completed.append(event_id)

    async def release_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> None:
        self.released.append((event_id, error))


class _Pipeline:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.processed: list[str] = []

    async def process_admitted_event(self, event_id: str) -> None:
        self.processed.append(event_id)
        if self.fail:
            raise RuntimeError("planner unavailable")


async def test_worker_completes_claimed_ingress() -> None:
    storage = _Storage([_work()])
    pipeline = _Pipeline()
    worker = DurableIngressWorker(storage=storage, pipeline=pipeline)

    assert await worker.run_once() == 1
    assert pipeline.processed == ["evt-1"]
    assert storage.completed == ["evt-1"]
    assert storage.released == []


async def test_worker_releases_failed_ingress_for_retry() -> None:
    storage = _Storage([replace(_work(), attempts=3)])
    pipeline = _Pipeline(fail=True)
    worker = DurableIngressWorker(storage=storage, pipeline=pipeline)

    assert await worker.run_once() == 0
    assert storage.completed == []
    assert storage.released == [("evt-1", "RuntimeError: planner unavailable")]
    assert worker.failures == 1
