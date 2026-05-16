"""End-to-end alpha walkthrough test following docs/runbooks/alpha-walkthrough.md.

Section 1 (fake-only path) exercised in full: config validation → smoke →
inspect → trace → evidence → retry → replay → final snapshot.

No Docker, no network, no SDKs.  Every test uses the shipped
``examples/configs/fake-bridge-smoke.toml`` via ``run_fake_bridge_smoke``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.smoke import run_fake_bridge_smoke
from medre.runtime.snapshot import build_runtime_snapshot, SCHEMA_VERSION
from medre.runtime.timeline import (
    assemble_event_timeline,
    assemble_storage_summary,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(_ROOT / "examples" / "configs" / "fake-bridge-smoke.toml")


# ---------------------------------------------------------------------------
# Shared smoke report fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def smoke_report(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the smoke once for the whole module and return the report."""
    db_path = str(tmp_path_factory.mktemp("alpha") / "walkthrough.db")
    report = await run_fake_bridge_smoke(CONFIG_PATH, storage_path=db_path)
    assert report["status"] == "passed", (
        f"Smoke must pass before walkthrough tests can proceed: "
        f"{report.get('fail_reasons', [])}"
    )
    return report


# ===========================================================================
# Test 1: Config validates
# ===========================================================================


class TestAlphaConfigValidation:
    """Section 1.1: medre config check — config parses, adapters and routes
    validate."""

    def test_config_loads_without_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, _paths = load_config(CONFIG_PATH)
        assert config.runtime.name == "fake-bridge-smoke"

    def test_routes_validate_at_least_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, _paths = load_config(CONFIG_PATH)
        assert len(config.routes.routes) >= 1


# ===========================================================================
# Tests 2–5: Smoke, inspect, trace, evidence
# ===========================================================================


class TestAlphaSmokeInspectTrace:
    """Sections 1.2–1.4: smoke, inspect receipts, trace event, evidence."""

    def test_smoke_report_passed(self, smoke_report: dict[str, Any]) -> None:
        assert smoke_report["status"] == "passed"

    def test_smoke_event_id_present(self, smoke_report: dict[str, Any]) -> None:
        event_id: str = smoke_report["event_id"]
        assert isinstance(event_id, str) and len(event_id) > 0

    def test_smoke_receipt_count_at_least_one(self, smoke_report: dict[str, Any]) -> None:
        receipts = smoke_report["delivery_receipts"]
        assert isinstance(receipts, list) and len(receipts) >= 1

    @pytest.mark.asyncio
    async def test_inspect_receipts_via_storage(
        self, smoke_report: dict[str, Any],
    ) -> None:
        """Storage API returns at least one receipt with status 'sent'."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        await storage.initialize()
        try:
            event_id = smoke_report["event_id"]
            receipts = await storage.list_receipts_for_event(event_id)
            assert len(receipts) >= 1
            sent = [r for r in receipts if r.status == "sent"]
            assert len(sent) >= 1
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_trace_event_timeline(
        self, smoke_report: dict[str, Any],
    ) -> None:
        """assemble_event_timeline returns event with at least 1 receipt."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        await storage.initialize()
        try:
            event_id = smoke_report["event_id"]
            timeline = await assemble_event_timeline(storage, event_id)
            assert timeline is not None
            assert timeline["event"] is not None
            assert len(timeline["receipts"]) >= 1
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_evidence_bundle(
        self, smoke_report: dict[str, Any],
    ) -> None:
        """assemble_storage_summary shows event_count >= 1, receipt_count >= 1."""
        storage_path = smoke_report["storage_path"]
        assert storage_path is not None

        from medre.core.storage.sqlite import SQLiteStorage

        storage = SQLiteStorage(db_path=storage_path)
        await storage.initialize()
        try:
            summary = await assemble_storage_summary(storage)
            assert summary["event_count"] >= 1
            assert summary["receipt_count"] >= 1
        finally:
            await storage.close()


# ===========================================================================
# Test 6: Retry scenario
# ===========================================================================


class TestAlphaRetryScenario:
    """Retry path: inject event to transient-failing adapter, retry via
    real RetryWorker._process_due(), verify both receipts."""

    @pytest.mark.asyncio
    async def test_retry_walkthrough(self, tmp_path: Path) -> None:
        from medre.adapters.base import AdapterDeliveryResult
        from medre.adapters.fake_matrix import FakeMatrixAdapter
        from medre.config.model import (
            AdapterConfigSet,
            MatrixRuntimeConfig,
            RetryConfig,
            RuntimeConfig,
            StorageConfig,
        )
        from medre.config.paths import MedrePaths
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
                "medre.runtime.routes", fromlist=["RouteConfigSet"]
            ).RouteConfigSet(
                routes=(
                    __import__(
                        "medre.runtime.routes", fromlist=["RouteConfig"]
                    ).RouteConfig(
                        route_id="retry_route",
                        source_adapters=("source",),
                        dest_adapters=("target",),
                    ),
                ),
            ),
        )

        # Build the app (RuntimeBuilder wires all subsystems)
        app = RuntimeBuilder(config, paths).build()

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

            # The pipeline sets next_retry_at only when the delivery plan has
            # an explicit retry_policy.  Set it here so the RetryWorker can
            # discover the failed receipt via list_due_retry_receipts.
            receipts = await app.storage.list_receipts_for_event(event.event_id)
            failed = [r for r in receipts if r.status == "failed"]
            assert len(failed) == 1
            await app.storage.update_retry_due(
                failed[0].receipt_id,
                datetime.now(timezone.utc) - timedelta(seconds=1),
            )

            # Stop the background polling loop before manually driving
            # _process_due to prevent double-processing the same receipt.
            await retry_worker.stop()
            retry_worker._shutdown_event.clear()

            # Drive retry: call _process_due with a far-future timestamp so
            # the failed receipt is guaranteed to be due.
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
        from medre.core.events.canonical import CanonicalEvent, EventMetadata
        from medre.core.storage.replay import (
            ReplayEngine,
            ReplayMode,
            ReplayRequest,
            collect_replay_summary,
        )

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
            routes=__import__("medre.runtime.routes", fromlist=["RouteConfigSet"]).RouteConfigSet(
                routes=(
                    __import__("medre.runtime.routes", fromlist=["RouteConfig"]).RouteConfig(
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


# ===========================================================================
# Test 8: Final snapshot
# ===========================================================================


class TestAlphaFinalSnapshot:
    """Build runtime snapshot and verify schema_version, accounting, lifecycle.

    The smoke report embeds a subset of the full runtime snapshot.  We verify
    the top-level contract: schema_version == 1 and accounting section present.
    The full lifecycle/runtime_state contract is verified separately by building
    a fresh runtime with retry disabled.
    """

    def test_snapshot_schema_version(self, smoke_report: dict[str, Any]) -> None:
        snap = smoke_report["snapshot"]
        assert snap["schema_version"] == SCHEMA_VERSION

    def test_snapshot_accounting_section_present(self, smoke_report: dict[str, Any]) -> None:
        snap = smoke_report["snapshot"]
        assert "accounting" in snap

    @pytest.mark.asyncio
    async def test_snapshot_lifecycle_runtime_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """build_runtime_snapshot works with retry disabled, lifecycle section present."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config, _source, paths = load_config(CONFIG_PATH)
        app = RuntimeBuilder(config, paths).build()
        await app.start()
        try:
            snap = build_runtime_snapshot(app)
            assert snap["schema_version"] == 1
            assert "lifecycle" in snap
            assert snap["lifecycle"]["runtime_state"] in (
                "running", "initialized", "starting",
            )
            # Retry section exists with disabled defaults
            assert "retry" in snap
            assert snap["retry"]["enabled"] is False
            assert snap["retry"]["running"] is False
        finally:
            await app.stop()
