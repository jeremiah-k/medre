"""Runtime-level retry and replay integration tests.

These tests exercise internal runtime APIs (RetryWorker._process_due,
ReplayEngine, RuntimeBuilder) rather than operator CLI commands.
They are valuable for deterministic verification but are NOT
operator walkthrough tests. For operator-facing walkthrough tests,
see test_alpha_walkthrough_cli.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.core.planning.delivery_plan import DeliveryPlan
from medre.runtime.builder import RuntimeBuilder

# ===========================================================================
# Test 6: Retry scenario
# ===========================================================================


class TestAlphaRetryScenario:
    """Retry path: inject event to transient-failing adapter, retry via
    real RetryWorker._process_due(), verify both receipts."""

    @pytest.mark.asyncio
    async def test_retry_walkthrough(self, tmp_path: Path) -> None:
        from medre.adapters.fakes.matrix import FakeMatrixAdapter
        from medre.config.model import (
            AdapterConfigSet,
            MatrixRuntimeConfig,
            RetryConfig,
            RuntimeConfig,
            StorageConfig,
        )
        from medre.config.paths import MedrePaths
        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.events.canonical import CanonicalEvent, EventMetadata

        # -- Transient-failing adapter: fails first deliver(), succeeds after --
        class _TransientThenSucceed(FakeMatrixAdapter):
            def __init__(self, adapter_id: str = "target") -> None:
                super().__init__(adapter_id=adapter_id)
                self._call_count = 0

            async def deliver(self, result: Any) -> AdapterDeliveryResult | None:
                self._call_count += 1
                if self._call_count <= 1:
                    raise ConnectionError("transient walkthrough failure")
                return await super().deliver(result)

        # -- Paths --
        db_path = tmp_path / "retry_walkthrough.db"
        paths = MedrePaths(
            config_dir=tmp_path / "cfg",
            config_file=tmp_path / "cfg" / "c.toml",
            state_dir=tmp_path / "state",
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            database_path=db_path,
        )
        for d in (paths.state_dir, paths.data_dir, paths.cache_dir, paths.log_dir):
            d.mkdir(parents=True, exist_ok=True)

        # -- Config with retry enabled and two fake adapters --
        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            retry=RetryConfig(enabled=True, interval_seconds=1.0),
            adapters=AdapterConfigSet(
                matrix={
                    "source": MatrixRuntimeConfig(
                        adapter_id="source",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    ),
                    "target": MatrixRuntimeConfig(
                        adapter_id="target",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    ),
                },
            ),
            routes=__import__(
                "medre.config.routes", fromlist=["RouteConfigSet"]
            ).RouteConfigSet(
                routes=(
                    __import__(
                        "medre.config.routes", fromlist=["RouteConfig"]
                    ).RouteConfig(
                        route_id="retry_route",
                        source_adapters=("source",),
                        dest_adapters=("target",),
                    ),
                ),
            ),
        )

        # Build the app (RuntimeBuilder wires all subsystems)
        from medre.core.planning.delivery_plan import RetryPolicy

        app = RuntimeBuilder(config, paths).build()

        # Replace the fallback resolver with one that sets a retry_policy so
        # the pipeline schedules retry_wait on transient failure instead of
        # dead-lettering the outbox item.
        from medre.core.planning.fallback_resolution import (
            FallbackResolver as _BaseFallback,
        )

        class _FallbackWithRetry(_BaseFallback):
            def resolve_fallback(
                self, event, target, capabilities, **kwargs
            ) -> DeliveryPlan:
                plan = super().resolve_fallback(event, target, capabilities, **kwargs)
                plan.retry_policy = RetryPolicy(max_attempts=3)
                return plan

        app.pipeline_runner._config.fallback_resolver = _FallbackWithRetry()

        # Swap the target adapter with transient-failing version.
        # The adapters dict is shared between PipelineRunner and MedreApp.
        app.adapters["target"] = _TransientThenSucceed(adapter_id="target")

        await app.start()
        try:
            # RetryWorker is started by app.start() when retry is enabled.
            retry_worker = app._retry_worker
            assert retry_worker is not None

            event = CanonicalEvent(
                event_id="evt-walkthrough-retry",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="source",
                source_transport_id="fake-transport",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"body": "walkthrough retry test"},
                metadata=EventMetadata(),
            )

            # Inject event — first delivery triggers transient failure.
            outcomes = await app.pipeline_runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            # Stop the background polling loop before manually driving
            # _process_due to prevent double-processing.
            await retry_worker.stop()
            retry_worker._shutdown_event.clear()

            # Drive retry: call _process_due with a far-future timestamp so
            # the retry_wait outbox item is guaranteed to be due.
            future_now = datetime.now(timezone.utc) + timedelta(days=365)
            await retry_worker._process_due(future_now)

            # Verify both receipts: one failed (original), one sent (retry).
            all_receipts = await app.storage.list_receipts_for_event(
                event.event_id,
            )
            assert len(all_receipts) == 2
            statuses = {r.status for r in all_receipts}
            assert statuses == {"failed", "sent"}
        finally:
            await app.stop()


# ===========================================================================
# Test 7: Replay scenario
# ===========================================================================


class TestAlphaReplayScenario:
    """Replay path: inject event, verify receipt, replay via ReplayEngine,
    verify replay receipt has source='replay'."""

    @pytest.mark.asyncio
    async def test_replay_walkthrough(self, tmp_path: Path) -> None:
        from medre.config.model import (
            AdapterConfigSet,
            MatrixRuntimeConfig,
            RuntimeConfig,
            StorageConfig,
        )
        from medre.config.paths import MedrePaths
        from medre.core.engine.replay.engine import ReplayEngine
        from medre.core.engine.replay.summary import collect_replay_summary
        from medre.core.engine.replay.types import ReplayMode, ReplayRequest
        from medre.core.events.canonical import CanonicalEvent, EventMetadata

        db_path = tmp_path / "replay_walkthrough.db"
        paths = MedrePaths(
            config_dir=tmp_path / "cfg",
            config_file=tmp_path / "cfg" / "c.toml",
            state_dir=tmp_path / "state",
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            log_dir=tmp_path / "logs",
            database_path=db_path,
        )
        for d in (paths.state_dir, paths.data_dir, paths.cache_dir, paths.log_dir):
            d.mkdir(parents=True, exist_ok=True)

        config = RuntimeConfig(
            storage=StorageConfig(backend="memory"),
            adapters=AdapterConfigSet(
                matrix={
                    "main": MatrixRuntimeConfig(
                        adapter_id="main",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    ),
                    "secondary": MatrixRuntimeConfig(
                        adapter_id="secondary",
                        enabled=True,
                        adapter_kind="fake",
                        config=None,
                    ),
                },
            ),
            routes=__import__(
                "medre.config.routes", fromlist=["RouteConfigSet"]
            ).RouteConfigSet(
                routes=(
                    __import__(
                        "medre.config.routes", fromlist=["RouteConfig"]
                    ).RouteConfig(
                        route_id="walkthrough_route",
                        source_adapters=("main",),
                        dest_adapters=("secondary",),
                    ),
                ),
            ),
        )

        app = RuntimeBuilder(config, paths).build()
        await app.start()

        try:
            event = CanonicalEvent(
                event_id="evt-walkthrough-replay",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="main",
                source_transport_id="fake-transport",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "walkthrough replay test"},
                metadata=EventMetadata(),
            )

            # Live delivery
            outcomes = await app.pipeline_runner.handle_ingress(event)
            assert len(outcomes) >= 1
            assert outcomes[0].status == "success"

            # Verify live receipt
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            live_receipts = [r for r in receipts if r.source == "live"]
            assert len(live_receipts) >= 1

            # Replay
            replay = ReplayEngine(
                storage=app.storage,
                pipeline=app.pipeline_runner,
                event_bus=app.event_bus,
                diagnostician=app.diagnostician,
            )
            request = ReplayRequest(
                mode=ReplayMode.BEST_EFFORT,
                run_id="walkthrough-replay-run",
                correlation_ids=[event.event_id],
            )
            summary = await collect_replay_summary(replay.replay(request))
            assert summary.events_replayed >= 1

            # Verify replay receipt has source="replay"
            all_receipts = await app.storage.list_receipts_for_event(
                event.event_id,
            )
            replay_receipts = [r for r in all_receipts if r.source == "replay"]
            assert len(replay_receipts) >= 1
        finally:
            await app.stop()
