"""Bounded shutdown and failed-startup fences for retained native dispatch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.contracts.adapter import AdapterHandoffResult
from medre.core.engine.pipeline import PipelineRunner
from medre.core.ingress import DurableIngressWorker
from medre.core.rendering.renderer import RenderingResult
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.routing import Router
from medre.runtime.errors import RuntimeShutdownError
from medre.runtime.retry import RetryWorker
from tests.helpers.async_utils import wait_until
from tests.helpers.delivery_runtime import _GatedTarget, _runtime
from tests.helpers.pipeline import make_event, make_pipeline_config_for_pipeline


async def test_runtime_cancels_stalled_send_within_shared_shutdown_budget(
    tmp_path: Path,
) -> None:
    """A cooperative native stall leaves an uncertain reservation, not a success."""
    blocked = _GatedTarget("FXLF")
    app = _runtime(
        tmp_path,
        {"FXLF": blocked, "matrix": FakePresentationAdapter(adapter_id="matrix")},
    )
    await app.start()
    store = app.storage
    assert store is not None
    try:
        await app._make_publish_inbound()(
            make_event(event_id="stalled", source_adapter="source")
        )
        await asyncio.wait_for(blocked.entered.wait(), 2)
        assert await wait_until(
            lambda: len(app.adapters["matrix"].delivered_payloads) == 1, timeout=2
        )
        await asyncio.wait_for(app.stop(), 1)
        assert not blocked.delivered_payloads
    finally:
        blocked.release.set()
        if app.pipeline_runner.running:
            await app.stop()
    reopened = SQLiteStorage(str(app.paths.database_path))
    await reopened.initialize()
    try:
        rows = await reopened.list_outbox_items_for_event("stalled")
        first = next(row for row in rows if row.target_adapter == "FXLF")
        assert first.status == "in_progress"
        assert first.active_attempt == 1
        assert first.attempt_number == 0
        assert not await reopened.list_receipts_for_outbox(first.outbox_id)
        assert (
            next(row for row in rows if row.target_adapter == "matrix").status == "sent"
        )
        restored = {
            name: FakePresentationAdapter(adapter_id=name)
            for name in ("FXLF", "matrix")
        }
        pipeline = PipelineRunner(
            make_pipeline_config_for_pipeline(reopened, Router(), restored)
        )
        dispatcher = RetryWorker(
            reopened, pipeline, None, initial_dispatch=True, enabled=False
        )
        # Even after the lease can expire, disabling automatic retries must
        # leave an interrupted positive reservation held, not redispatch it.
        await dispatcher._process_due(
            datetime.now(timezone.utc) + timedelta(seconds=120)
        )
        assert await reopened.list_outbox_items_for_event("stalled") == rows
        assert all(not adapter.delivered_payloads for adapter in restored.values())
    finally:
        await reopened.close()


class _CancellationResistantTarget(_GatedTarget):
    """Model an SDK operation that retains ownership after caller cancellation."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(adapter_id)
        self.cancelled = asyncio.Event()

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return await FakePresentationAdapter.deliver(self, result)


async def test_runtime_retains_dependencies_until_resistant_native_send_exits(
    tmp_path: Path,
) -> None:
    """A bounded shutdown refusal cannot release a still-running native owner."""

    blocked = _CancellationResistantTarget("FXLF")
    app = _runtime(tmp_path, {"FXLF": blocked})
    await app.start()
    try:
        await app._make_publish_inbound()(
            make_event(event_id="resistant", source_adapter="source")
        )
        await asyncio.wait_for(blocked.entered.wait(), 2)
        with pytest.raises(
            RuntimeShutdownError, match="Outbox dispatch remains active"
        ):
            await asyncio.wait_for(app.stop(), 1)
        assert blocked.cancelled.is_set()
        assert app.pipeline_runner.running
        assert app.storage is not None
        assert await app.storage.get("resistant") is not None
        assert blocked._started
        assert app._retry_worker.has_retained_work
        assert blocked.calls == 1
    finally:
        blocked.release.set()
        assert await wait_until(
            lambda: not app._retry_worker.has_retained_work, timeout=2
        )
        await app.stop()
    assert len(blocked.delivered_payloads) == 1


async def test_cancelled_startup_retains_dependencies_of_started_native_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    """Cancellation after worker activation must quiesce sends before adapters."""

    blocked = _CancellationResistantTarget("FXLF")
    app = _runtime(tmp_path, {"FXLF": blocked})
    app.paths.state_dir.mkdir(parents=True, exist_ok=True)
    await app.storage.initialize()
    await app.pipeline_runner.admit_ingress(
        make_event(event_id="startup", source_adapter="source"), "live"
    )
    await app.pipeline_runner.enqueue_admitted_event("startup")
    await app.storage.close()

    async def cancelled_start(_worker):
        await asyncio.wait_for(blocked.entered.wait(), 2)
        raise asyncio.CancelledError("cancelled at the activation boundary")

    with monkeypatch.context() as patch:
        patch.setattr(DurableIngressWorker, "start", cancelled_start)
        with pytest.raises(asyncio.CancelledError):
            await app.start()
    try:
        assert blocked.cancelled.is_set()
        assert blocked._started
        assert app.pipeline_runner.running
        assert await app.storage.get("startup") is not None
        assert app._retry_worker.has_retained_work
    finally:
        blocked.release.set()
        assert await wait_until(
            lambda: not app._retry_worker.has_retained_work, timeout=2
        )
        await app.stop()
