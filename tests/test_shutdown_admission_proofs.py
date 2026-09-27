"""Shutdown admission ordering proofs.

The durable-ingress shutdown handoff promises two invariants:

1. Late adapter callbacks may still cross durable admission while adapters
   are shutting down — their rows stay pending for the next runtime
   generation. No callback can start durable work only after inbound
   acceptance closes, which happens after the last adapter stops.
2. No accepted work loses ownership during drain: an admission already
   crossing when shutdown begins completes before the pipeline runner and
   storage go away.

These tests pin both invariants at the ``MedreApp.stop()`` level with a
real capacity controller, controllable admission and storage doubles, and
deterministic event signalling — no fixed sleeps, no network.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from medre.config.model import RuntimeLimits
from medre.core.supervision.capacity import (
    CapacityController,
    InboundAdmissionRejected,
)
from medre.runtime.app import MedreApp, RuntimeState
from tests.helpers.async_utils import wait_until


class _BlockingAdapter:
    """Fake adapter whose stop() is observable and optionally blocking."""

    platform = "fake"

    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        self._block = block
        self.entered_stop = asyncio.Event()
        self.stop_calls = 0

    async def stop(self, timeout: float | None = None) -> None:
        self.stop_calls += 1
        self.entered_stop.set()
        if self._block is not None:
            await self._block.wait()


class _ProofRunner:
    """Pipeline runner double recording admission and teardown order.

    Admissions refuse to start once storage has closed, so the proofs
    fail loudly if shutdown ever tears storage down before an admitted
    arrival's crossing begins.
    """

    conversation_projection_repair_failed = False

    def __init__(
        self,
        order: list[str],
        gate: asyncio.Event | None = None,
        storage: "_ProofStorage | None" = None,
    ) -> None:
        self._order = order
        self._gate = gate
        self._storage = storage
        self.admitted: list[Any] = []

    async def admit_ingress(
        self, event: Any, provenance: Any, **_: Any
    ) -> dict[str, bool]:
        if self._storage is not None and self._storage.closed:
            raise RuntimeError("admission attempted after storage closed")
        if self._gate is not None:
            await self._gate.wait()
            if self._storage is not None and self._storage.closed:
                raise RuntimeError("storage closed while admission in flight")
        self.admitted.append((event, provenance))
        self._order.append("admitted")
        return {"ok": True}

    async def stop(self) -> None:
        self._order.append("runner_stopped")

    async def mark_conversation_projection_clean(self) -> None:
        self._order.append("projection_clean")


class _RecordingController(CapacityController):
    """Capacity controller recording inbound slot release order.

    The drain proof needs the full gate lifecycle — crossing *and* slot
    release — ahead of storage teardown, not just the crossing itself.
    """

    def __init__(self, limits: RuntimeLimits, order: list[str]) -> None:
        super().__init__(limits)
        self._proof_order = order

    async def release_inbound(self) -> None:
        await super().release_inbound()
        self._proof_order.append("released")


class _ProofStorage:
    """Storage double recording close order."""

    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self._order.append("storage_closed")


def _proof_app(
    runner: _ProofRunner,
    storage: _ProofStorage,
    adapter: _BlockingAdapter,
    order: list[str],
) -> MedreApp:
    limits = RuntimeLimits(shutdown_drain_timeout_seconds=3)
    app = MedreApp(
        config=SimpleNamespace(
            runtime=SimpleNamespace(name="proof", shutdown_timeout_seconds=2),
            limits=limits,
        ),
        paths=MagicMock(),
        storage=storage,
        rendering_pipeline=MagicMock(),
        router=MagicMock(),
        fallback_resolver=MagicMock(),
        relation_resolver=MagicMock(),
        pipeline_runner=runner,
        diagnostician=MagicMock(),
        adapters={"src": adapter},
        shutdown_event=asyncio.Event(),
        event_bus=MagicMock(),
    )
    app._state = RuntimeState.RUNNING
    app._event_buffer = SimpleNamespace(emit=lambda *a, **k: None)
    app._replay_engine = None
    app._ingress_worker = None
    app._retry_worker = None
    app._attachment_permits = None
    app._capacity_controller = _RecordingController(limits, order)
    app.started_adapter_ids = ["src"]
    app._adapter_states = {}
    app._storage_initialized = True
    return app


async def _stop_clean(app: MedreApp) -> None:
    await app.stop()


class TestShutdownAdmissionOrdering:
    """MedreApp.stop() keeps the documented inbound admission handoff."""

    async def test_late_callback_admits_while_adapter_is_stopping(self) -> None:
        """A callback crossing during adapter teardown still admits.

        Delivery acceptance has closed by then, but inbound acceptance is
        deliberately open until the last adapter stops, so the row commits
        and stays durable for the next runtime generation.
        """
        order: list[str] = []
        adapter_block = asyncio.Event()
        adapter = _BlockingAdapter(block=adapter_block)
        storage = _ProofStorage(order)
        app = _proof_app(_ProofRunner(order, storage=storage), storage, adapter, order)
        publish = app._make_publish_inbound()

        stop_task = asyncio.create_task(_stop_clean(app))
        assert await wait_until(adapter.entered_stop.is_set)
        # The adapter-stop window: delivery acceptance is already closed.
        assert app._capacity_controller.accepting_work is False
        assert app._capacity_controller.inbound_accepting is True

        await publish("late-callback-event")

        adapter_block.set()
        await asyncio.wait_for(stop_task, timeout=5)
        assert app._capacity_controller.inbound_accepting is False
        assert app.pipeline_runner.admitted == [("late-callback-event", "live")]

    async def test_callback_after_full_stop_is_rejected_and_counted(self) -> None:
        """After stop() completes, no callback can start durable work."""
        order: list[str] = []
        storage = _ProofStorage(order)
        runner = _ProofRunner(order, storage=storage)
        app = _proof_app(runner, storage, _BlockingAdapter(), order)
        publish = app._make_publish_inbound()

        await _stop_clean(app)

        with pytest.raises(InboundAdmissionRejected):
            await publish("post-stop-event")
        snapshot = app._capacity_controller.snapshot()
        assert snapshot["inbound_accepting"] is False
        assert snapshot["inbound_rejections"] == 1
        assert runner.admitted == []

    async def test_storage_closes_only_after_inflight_admission_drains(self) -> None:
        """An admission crossing at shutdown completes before storage closes."""
        order: list[str] = []
        gate = asyncio.Event()
        storage = _ProofStorage(order)
        runner = _ProofRunner(order, gate=gate, storage=storage)
        app = _proof_app(runner, storage, _BlockingAdapter(), order)
        publish = app._make_publish_inbound()

        crossing = asyncio.create_task(publish("inflight-event"))
        assert await wait_until(
            lambda: app._capacity_controller.snapshot()["inbound_current"] == 1
        )

        stop_task = asyncio.create_task(_stop_clean(app))
        # The adapter stopped instantly; the drain holds the shutdown open
        # while the admission is still crossing.
        assert await wait_until(
            lambda: app._capacity_controller.snapshot()["inbound_accepting"] is False
        )
        assert storage.closed is False

        gate.set()
        await asyncio.wait_for(crossing, timeout=5)
        await asyncio.wait_for(stop_task, timeout=5)

        assert order.index("admitted") < order.index("released")
        assert order.index("released") < order.index("storage_closed")
        assert storage.closed is True

    async def test_undrained_admission_leaves_projection_dirty(self) -> None:
        """An admission still crossing at the drain deadline is visible.

        The shutdown completes (storage still closes), but the projection
        startup marker is not marked clean, so the next run repairs.
        """
        order: list[str] = []
        gate = asyncio.Event()
        storage = _ProofStorage(order)
        runner = _ProofRunner(order, gate=gate, storage=storage)
        limits = RuntimeLimits(shutdown_drain_timeout_seconds=0.2)
        app = _proof_app(runner, storage, _BlockingAdapter(), order)
        app.config.limits = limits
        app._capacity_controller = CapacityController(limits)
        publish = app._make_publish_inbound()

        crossing = asyncio.create_task(publish("stuck-event"))
        assert await wait_until(
            lambda: app._capacity_controller.snapshot()["inbound_current"] == 1
        )

        stop_task = asyncio.create_task(_stop_clean(app))
        await asyncio.wait_for(stop_task, timeout=5)

        assert storage.closed is True
        assert "projection_clean" not in order
        assert "admitted" not in order

        gate.set()
        with contextlib.suppress(asyncio.CancelledError, RuntimeError):
            await asyncio.wait_for(crossing, timeout=1)
