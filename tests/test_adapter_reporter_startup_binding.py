# medre-test-tier: compatibility
"""Startup binding of the producer-pressure reporter through a real adapter.

The collector lifecycle suite stays hermetic for core-only tiers; this
module carries the one qualification that cannot: proving the sink a
real adapter receives at startup is callable, attributed to that
adapter, and wired to the durable pressure accounting.  It builds a
real runtime with a fake-mode radio adapter through ``RuntimeBuilder``,
captures the ``AdapterContext`` the adapter's own ``start`` receives,
reports through the bound ``report_producer_pressure`` sink, and checks
the fact lands in storage after one absorb-and-flush cycle.

The compatibility tier declaration is required because the runtime
config for the adapter is provided by the adapter distribution itself;
without that distribution installed the module import cannot resolve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medre.core.contracts.adapter import ProducerPressureReport
from medre.core.storage.sqlite.storage import SQLiteStorage

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_ADAPTER_ID = "radio-session"


def _report(outcome: str, count: int) -> ProducerPressureReport:
    """Build one content-free producer report for the session adapter."""
    return ProducerPressureReport(adapter_id=_ADAPTER_ID, outcome=outcome, count=count)


async def _absorb_and_flush(app: Any) -> None:
    """Run one absorb epoch, then await the single-flight flush task."""
    app._absorb_producer_pressure()
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None and not task.done():
        await task


async def _durable_total(storage: SQLiteStorage, outcome: str) -> int:
    """Fold durable pressure rows of one outcome into a total count."""
    rows = await storage.list_inbound_pressure_observations()
    assert all(row["source"] == _ADAPTER_ID for row in rows)
    return sum(int(row["count"]) for row in rows if row["outcome"] == outcome)


async def test_adapter_start_binds_a_usable_pressure_reporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adapter receives a working, attributed sink through real startup."""
    from medre.config.adapters.meshtastic_runtime import MeshtasticRuntimeConfig
    from medre.config.model import AdapterConfigSet, RuntimeConfig, StorageConfig
    from medre.config.paths import resolve
    from medre.runtime.builder import RuntimeBuilder

    config = RuntimeConfig(
        storage=StorageConfig(backend="sqlite", path=str(tmp_path / "startup.db")),
        adapters=AdapterConfigSet(
            meshtastic={
                _ADAPTER_ID: MeshtasticRuntimeConfig(
                    adapter_id=_ADAPTER_ID, adapter_kind="fake", enabled=True
                )
            }
        ),
    )
    app = RuntimeBuilder(config, resolve(environ={"MEDRE_HOME": str(tmp_path)})).build()
    adapter = app.adapters[_ADAPTER_ID]
    original_start = adapter.start
    captured = []

    async def capture_context(ctx):
        captured.append(ctx)
        assert callable(ctx.report_producer_pressure)
        ctx.report_producer_pressure(_report("overflow", 3))
        await original_start(ctx)

    monkeypatch.setattr(adapter, "start", capture_context)
    try:
        await app.start()
        assert app.started_adapter_ids == [_ADAPTER_ID]
        assert len(captured) == 1
        await _absorb_and_flush(app)
        assert await _durable_total(app.storage, "overflow") == 3
    finally:
        await app.stop()
