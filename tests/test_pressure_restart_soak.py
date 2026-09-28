"""Sustained pre-admission pressure with restart survival across cycles.

Env-scalable soak: ``MEDRE_PRESSURE_SOAK_CYCLES`` (default 3, range
1–10 000). Each cycle refuses a burst of arrivals at the inbound
admission gate against a production-shaped runtime with real SQLite
storage, flushes the durable pressure evidence, restarts the storage
generation, and verifies the aggregates survived and the new generation
appends. The default count keeps the always-on suite fast; long
unattended campaigns raise the count through the environment.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.capacity import (
    CapacityController,
    InboundAdmissionRejected,
)
from tests.helpers.async_utils import wait_until
from tests.helpers.inbound_gate import GateAppDouble, LimitsDouble

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_REFUSALS_PER_CYCLE = 5


class _BlockingRunner:
    """Runner whose first admission blocks until the test releases it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.admitted: list[object] = []

    async def admit_ingress(
        self,
        event: object,
        provenance: object,
        attachment: object = None,
        attachment_limits: object = None,
    ) -> object:
        self.admitted.append(event)
        await self.release.wait()
        return {"admitted": len(self.admitted)}


_CYCLES = int(os.environ.get("MEDRE_PRESSURE_SOAK_CYCLES", "3"))
assert 1 <= _CYCLES <= 10_000, "MEDRE_PRESSURE_SOAK_CYCLES must be 1..10000"


class TestPressureRestartSoak:
    """Refuse → flush → restart → verify, repeated across generations."""

    async def test_pressure_evidence_survives_repeated_restarts(
        self, tmp_path: Path
    ) -> None:
        db_path = str(tmp_path / "pressure-soak.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        try:
            for cycle in range(1, _CYCLES + 1):
                controller = CapacityController(
                    LimitsDouble(
                        max_inflight_inbound_admissions=1,
                        inbound_admission_timeout_seconds=0.01,
                    )
                )
                app = GateAppDouble()
                runner = _BlockingRunner()
                app.pipeline_runner = runner
                app.storage = storage
                app._capacity_controller = controller

                publish = app._make_publish_inbound("radio")
                admitted = asyncio.create_task(publish(f"cycle-{cycle}-admit"))
                assert await wait_until(lambda c=controller: c.inbound_current == 1)

                refused_here = 0
                for i in range(_REFUSALS_PER_CYCLE):
                    try:
                        await publish(f"cycle-{cycle}-refuse-{i}")
                    except InboundAdmissionRejected:
                        refused_here += 1
                assert refused_here == _REFUSALS_PER_CYCLE
                runner.release.set()
                await admitted

                flush_task = getattr(app, "_pressure_flush_task", None)
                if flush_task is not None:
                    await flush_task
                await app._drain_inbound_pressure_flush()

                rows = await storage.list_inbound_pressure_observations(source="radio")
                assert rows, f"cycle {cycle}: no durable pressure rows"
                total = sum(r["count"] for r in rows)
                assert total == cycle * _REFUSALS_PER_CYCLE, (
                    f"cycle {cycle}: expected cumulative count "
                    f"{cycle * _REFUSALS_PER_CYCLE}, got {total}"
                )
                for row in rows:
                    assert row["outcome"] in ("timed_out", "rejected")
                    assert row["count"] > 0
                    assert row["first_seen_at"] <= row["last_seen_at"]

                # The gate returns to a quiescent state every cycle: no
                # leaked waiters or slots, no stranded pending counters.
                snapshot = controller.snapshot()
                assert snapshot["inbound_current"] == 0
                assert snapshot["inbound_admission_waiting"] == 0
                assert not (getattr(app, "_pressure_pending", None) or {})

                # Generation change: reopen storage and verify the whole
                # multi-generation history is still readable.
                if cycle < _CYCLES:
                    await storage.close()
                    storage = SQLiteStorage(db_path)
                    await storage.initialize()
                    survived = await storage.list_inbound_pressure_observations(
                        source="radio"
                    )
                    assert sum(r["count"] for r in survived) == (
                        cycle * _REFUSALS_PER_CYCLE
                    )
        finally:
            await storage.close()

    async def test_single_generation_accumulates_across_windows(
        self, tmp_path: Path
    ) -> None:
        """One generation appends distinct windows without history loss."""
        storage = SQLiteStorage(str(tmp_path / "windows.db"))
        await storage.initialize()
        try:
            base = 1_800_000_000
            for i in range(6):
                await storage.record_inbound_pressure(
                    "radio",
                    "rejected",
                    count=2,
                    unix_seconds=base + i * 90,
                )
            rows = await storage.list_inbound_pressure_observations()
            assert len(rows) == 6
            starts = [r["window_start"] for r in rows]
            assert starts == sorted(starts)
            assert len(set(starts)) == 6
        finally:
            await storage.close()
