"""Drain abandoned evidence persistence tests.

Split from test_runtime_cancellation.py to keep it under the 1500-line limit.

Covers:
- Drain timeout persists structured abandoned-work receipts to storage.
- Abandoned receipt failure_kind_detail via delivery_receipt_to_report_dict.

Uses no real transport dependencies; all adapters are fake/stub.
Does not overlap with test_runtime_hygiene.py or test_runtime_recovery.py.
"""

from __future__ import annotations

import gc
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at a temp directory."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_matrix_config(adapter_id: str = "fake_matrix") -> MatrixRuntimeConfig:
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=True,
        adapter_kind="fake",
        config=None,
    )


def _build_app(config: RuntimeConfig, paths: MedrePaths):
    """Build a MedreApp via RuntimeBuilder."""
    return RuntimeBuilder(config, paths).build()


# =====================================================================
# Drain abandoned evidence persistence
# =====================================================================


class TestDrainAbandonedEvidencePersistence:
    """Drain timeout persists structured abandoned-work receipts to storage."""

    @pytest.mark.asyncio
    async def test_stop_with_inflight_deliveries_persists_abandoned_receipts(
        self, tmp_paths: MedrePaths
    ) -> None:
        """When drain timeout expires with in-flight deliveries, abandoned
        receipts are persisted to storage with failure_kind=shutdown_rejection
        and error=shutdown_drain_timeout.
        """
        from medre.core.engine.pipeline import InflightDelivery
        from medre.core.events.canonical import CanonicalEvent
        from medre.core.events.kinds import EventKind
        from medre.core.events.metadata import EventMetadata

        # Use sqlite (not memory) so data survives storage.close().
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-abandoned-evidence"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_config()},
            ),
        )
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None
        storage = app.storage
        assert storage is not None

        # Remember the database path so we can reopen after close.
        db_path = str(tmp_paths.database_path)

        # Inject an event into storage.
        event = CanonicalEvent(
            event_id="evt-abandoned-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_matrix",
            source_transport_id="matrix",
            source_channel_id="test_room",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test abandoned evidence"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        # Acquire a delivery slot to simulate in-flight work.
        assert await cc.acquire_delivery()
        assert cc.delivery_current == 1

        # Manually register inflight tracking to test the evidence path.
        inflight_key = "evt-abandoned-001:route-1:fake_matrix:plan-1"
        app.pipeline_runner._inflight_deliveries[inflight_key] = InflightDelivery(
            event_id=event.event_id,
            route_id="route-1",
            target_adapter="fake_matrix",
            target_channel=None,
            delivery_plan_id="plan-1",
            source="live",
            replay_run_id=None,
            acquired_at=__import__("time").monotonic(),
        )

        # Use a zero drain timeout to trigger the timeout path immediately.
        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            await app.stop()
        finally:
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )
            # Defense-in-depth: ensure the app's storage connection is
            # closed even if stop() exited before reaching its internal
            # close step (e.g. due to an unexpected exception).
            if storage is not None:
                with suppress(Exception):
                    await storage.close()

        assert app.state == RuntimeState.STOPPED

        # Reopen storage to verify persisted receipts.
        from medre.core.storage.sqlite.storage import SQLiteStorage

        verify_storage = SQLiteStorage(db_path)
        try:
            await verify_storage.initialize()
            receipts = await verify_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1, (
                f"Expected at least 1 receipt for {event.event_id}, "
                f"got {len(receipts)}"
            )

            r = receipts[0]
            assert r.status == "suppressed", f"Expected 'suppressed', got '{r.status}'"
            assert (
                r.failure_kind == "shutdown_rejection"
            ), f"Expected 'shutdown_rejection', got '{r.failure_kind}'"
            assert (
                r.error == "shutdown_drain_timeout"
            ), f"Expected 'shutdown_drain_timeout', got '{r.error}'"
            assert r.event_id == event.event_id
            assert r.attempt_number == 1
            assert r.source == "live"
        finally:
            await verify_storage.close()
            del verify_storage
            gc.collect()

    @pytest.mark.asyncio
    async def test_abandoned_receipt_failure_kind_detail(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Abandoned receipt produces failure_kind_detail=shutdown_drain_timeout
        when processed through delivery_receipt_to_report_dict."""
        from medre.core.engine.pipeline import InflightDelivery
        from medre.core.events.canonical import CanonicalEvent
        from medre.core.events.kinds import EventKind
        from medre.core.events.metadata import EventMetadata
        from medre.runtime.reporting import delivery_receipt_to_report_dict

        # Use sqlite (not memory) so data survives storage.close().
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test-abandoned-detail"),
            storage=StorageConfig(backend="sqlite"),
            adapters=AdapterConfigSet(
                matrix={"main": _fake_matrix_config()},
            ),
        )
        app = _build_app(config, tmp_paths)
        await app.start()

        cc = app._capacity_controller
        assert cc is not None
        storage = app.storage
        assert storage is not None

        db_path = str(tmp_paths.database_path)

        event = CanonicalEvent(
            event_id="evt-abandoned-detail-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fake_matrix",
            source_transport_id="matrix",
            source_channel_id="test_room",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test detail"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        # Acquire and register inflight manually.
        assert await cc.acquire_delivery()
        inflight_key = "evt-abandoned-detail-001:route-1:fake_matrix:plan-1"
        app.pipeline_runner._inflight_deliveries[inflight_key] = InflightDelivery(
            event_id=event.event_id,
            route_id="route-1",
            target_adapter="fake_matrix",
            target_channel=None,
            delivery_plan_id="plan-1",
            source="live",
            replay_run_id=None,
            acquired_at=__import__("time").monotonic(),
        )

        original_drain = app.config.limits.shutdown_drain_timeout_seconds
        object.__setattr__(
            app.config.limits,
            "shutdown_drain_timeout_seconds",
            0,
        )

        try:
            await app.stop()
        finally:
            object.__setattr__(
                app.config.limits,
                "shutdown_drain_timeout_seconds",
                original_drain,
            )
            # Defense-in-depth: ensure the app's storage connection is
            # closed even if stop() exited before reaching its internal
            # close step.
            if storage is not None:
                with suppress(Exception):
                    await storage.close()

        assert app.state == RuntimeState.STOPPED

        # Reopen storage to verify persisted receipts.
        from medre.core.storage.sqlite.storage import SQLiteStorage

        verify_storage = SQLiteStorage(db_path)
        try:
            await verify_storage.initialize()
            receipts = await verify_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1

            report = delivery_receipt_to_report_dict(receipts[0])
            assert report["failure_kind"] == "shutdown_rejection"
            assert report["failure_kind_detail"] == "shutdown_drain_timeout"
            assert report["retryable"] is False
            assert report["status"] == "suppressed"
        finally:
            await verify_storage.close()
            del verify_storage
            gc.collect()
