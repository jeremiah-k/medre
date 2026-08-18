"""Durable ingress worker lifecycle, retry, and lease tests."""

from __future__ import annotations

import asyncio

from medre.core.ingress import DurableIngressWorker, IngressWorkItem
from tests.helpers.async_utils import wait_until


def _work(event_id: str = "evt-1", *, attempts: int = 1) -> IngressWorkItem:
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
        self.failed: list[tuple[str, str]] = []
        self.renewed: list[str] = []
        self.claim_limits: list[int] = []
        self.owns = True

    async def claim_ingress_work(self, **kwargs):
        limit = kwargs["limit"]
        self.claim_limits.append(limit)
        work, self.work = self.work[:limit], self.work[limit:]
        return work

    async def complete_ingress_work(self, event_id: str, *, worker_id: str) -> bool:
        if not self.owns:
            return False
        self.completed.append(event_id)
        return True

    async def release_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool:
        if not self.owns:
            return False
        self.released.append((event_id, error))
        return True

    async def fail_ingress_work(
        self, event_id: str, *, worker_id: str, error: str
    ) -> bool:
        if not self.owns:
            return False
        self.failed.append((event_id, error))
        return True

    async def renew_ingress_work_lease(
        self, event_id: str, *, worker_id: str, lease_seconds: float
    ) -> bool:
        self.renewed.append(event_id)
        return self.owns


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
    storage = _Storage([_work(attempts=3)])
    worker = DurableIngressWorker(
        storage=storage, pipeline=_Pipeline(fail=True), max_attempts=5
    )

    assert await worker.run_once() == 0
    assert storage.released == [("evt-1", "RuntimeError: planner unavailable")]
    assert storage.failed == []
    assert worker.failures == 1


async def test_worker_terminally_fails_poison_work_at_retry_budget() -> None:
    storage = _Storage([_work(attempts=5)])
    worker = DurableIngressWorker(
        storage=storage, pipeline=_Pipeline(fail=True), max_attempts=5
    )

    assert await worker.run_once() == 0
    assert storage.released == []
    assert storage.failed == [("evt-1", "RuntimeError: planner unavailable")]
    assert worker.terminal_failures == 1


async def test_worker_renews_lease_while_processing() -> None:
    storage = _Storage([_work()])
    entered = asyncio.Event()
    release = asyncio.Event()

    class _SlowPipeline:
        async def process_admitted_event(self, _event_id: str) -> None:
            entered.set()
            await release.wait()

    worker = DurableIngressWorker(
        storage=storage, pipeline=_SlowPipeline(), lease_seconds=0.06
    )
    task = asyncio.create_task(worker.run_once())
    await entered.wait()
    assert await wait_until(lambda: bool(storage.renewed), timeout=1, interval=0.005)
    release.set()
    assert await task == 1


async def test_worker_claims_each_item_only_when_ready_to_process() -> None:
    storage = _Storage([_work("evt-1"), _work("evt-2")])
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    processed: list[str] = []

    class _SequentialPipeline:
        async def process_admitted_event(self, event_id: str) -> None:
            processed.append(event_id)
            if event_id == "evt-1":
                first_started.set()
                await release_first.wait()

    worker = DurableIngressWorker(
        storage=storage,
        pipeline=_SequentialPipeline(),
        batch_size=2,
        lease_seconds=0.06,
    )
    task = asyncio.create_task(worker.run_once())
    await first_started.wait()

    assert storage.claim_limits == [1]
    assert processed == ["evt-1"]

    release_first.set()
    assert await task == 2
    assert storage.claim_limits == [1, 1]
    assert processed == ["evt-1", "evt-2"]


async def test_worker_does_not_count_completion_after_lease_loss() -> None:
    storage = _Storage([_work()])
    storage.owns = False
    worker = DurableIngressWorker(storage=storage, pipeline=_Pipeline())

    assert await worker.run_once() == 0
    assert worker.processed == 0
    assert worker.lost_leases == 1


async def test_worker_survives_claim_cycle_failure() -> None:
    class _FlakyStorage(_Storage):
        def __init__(self) -> None:
            super().__init__([_work()])
            self.claims = 0

        async def claim_ingress_work(self, **kwargs):
            self.claims += 1
            if self.claims == 1:
                raise RuntimeError("database busy")
            return await super().claim_ingress_work(**kwargs)

    storage = _FlakyStorage()
    worker = DurableIngressWorker(
        storage=storage, pipeline=_Pipeline(), interval_seconds=0.001
    )

    await worker.start()
    try:
        assert await wait_until(lambda: worker.processed > 0, timeout=1, interval=0.005)
    finally:
        await worker.stop()

    assert worker.failures == 1
    assert storage.claims >= 2
    assert storage.completed == ["evt-1"]
