"""Outbox-specific snapshot tests (split from test_runtime_snapshot.py).

Covers storage-backed outbox counts taking precedence over stale worker
cache, refresh-outbox-to-storage propagation, and the authoritative-flag
mechanism that prevents a retry worker's empty cache from overwriting
freshly refreshed storage counts in live snapshots.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.runtime.app import MedreApp
from medre.runtime.snapshot import build_runtime_snapshot
from tests.helpers.snapshot import make_fake_app


def _make_medre_app() -> MedreApp:
    """Build a minimal MedreApp for outbox property testing."""
    return MedreApp(
        config=MagicMock(),
        paths=MagicMock(),
        storage=None,
        event_bus=MagicMock(),
        rendering_pipeline=MagicMock(),
        router=MagicMock(),
        fallback_resolver=MagicMock(),
        relation_resolver=MagicMock(),
        pipeline_runner=MagicMock(),
        diagnostician=MagicMock(),
        adapters={},
        shutdown_event=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Tests: Outbox storage-backed counts
# ---------------------------------------------------------------------------
class TestOutboxStorageBackedCounts:
    """Outbox counts should reflect storage-seeded data."""

    def test_outbox_precedence_and_structure(self) -> None:
        """Outbox counts reflect storage-backed truth, not worker cache."""
        # 1) No state -> null counts.
        snap_empty = build_runtime_snapshot(make_fake_app(), snapshot_scope="build")
        assert snap_empty["outbox"]["counts"] == {}

        # 2) Storage-seeded counts used when worker cache is empty
        #    (simulates startup seeding which sets authoritative=True).
        app = make_fake_app()
        app._outbox_state = {"pending": 3, "retry_wait": 1}
        app._outbox_storage_authoritative = True
        app._retry_worker = MagicMock(outbox_counts={})
        snap = build_runtime_snapshot(app, snapshot_scope="build")
        assert snap["outbox"]["counts"] == {"pending": 3, "retry_wait": 1}
        assert snap["outbox"]["scope"] == "storage_seeded"

        # 3) Storage truth wins over stale worker cache
        #    (simulates refresh which sets authoritative=True).
        app2 = make_fake_app()
        app2._outbox_state = {"pending": 5, "sent": 10}
        app2._outbox_storage_authoritative = True
        app2._retry_worker = MagicMock(outbox_counts={"pending": 99})
        snap2 = build_runtime_snapshot(app2, snapshot_scope="build")
        assert snap2["outbox"]["counts"] == {"pending": 5, "sent": 10}

        # 4) Worker-only state -> worker cache counts used.
        app3 = make_fake_app()
        app3._retry_worker = MagicMock(outbox_counts={"pending": 7})
        assert build_runtime_snapshot(app3, snapshot_scope="build")["outbox"][
            "counts"
        ] == {"pending": 7}

        # 5) Structure check.
        assert set(snap["outbox"].keys()) == {"counts", "live_refresh", "note", "scope"}
        assert snap["outbox"]["live_refresh"] is False


class TestStorageBackedOutboxRefresh:
    """Regression: outbox rows must appear in snapshot via refresh."""

    @pytest.mark.asyncio
    async def test_refresh_populates_snapshot_from_storage(self, tmp_path) -> None:
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = tmp_path / "test_refresh.db"
        storage = SQLiteStorage(str(db_path))
        try:
            await storage.initialize()
            for i in range(3):
                await storage.create_outbox_item(
                    DeliveryOutboxItem(
                        outbox_id=f"obx-refresh-{i}",
                        event_id=f"evt-refresh-{i}",
                        route_id="route-refresh",
                        delivery_plan_id=f"plan-refresh-{i}",
                        target_adapter="adapter_refresh",
                        attempt_number=1,
                        status="pending",
                    )
                )
            app = make_fake_app(storage=storage)
            snap_before = build_runtime_snapshot(app, snapshot_scope="build")
            assert snap_before["outbox"]["counts"] == {}
            await app.refresh_outbox_state_from_storage()
            snap_after = build_runtime_snapshot(app, snapshot_scope="build")
            assert snap_after["outbox"]["counts"] == {"pending": 3}
            assert snap_after["outbox"]["scope"] == "storage_seeded"
        finally:
            await storage.close()


class TestStorageRefreshAuthoritativeOverWorkerCache:
    """Regression: storage-refreshed counts are authoritative for live snapshots.

    PC scenario: ``refresh_outbox_state_from_storage()`` is called before a
    snapshot, but the retry worker cache is ``{}`` (empty dict from a
    completed cycle that found no items).  Without the authoritative flag,
    ``outbox_state`` would overwrite the freshly refreshed storage counts
    with the worker's ``{}``.

    After the fix, the first read of ``outbox_state`` after refresh returns
    storage counts.  Subsequent reads resume normal worker-cache precedence
    (including ``{}`` as a valid worker result — no truthiness fallback).
    """

    @pytest.mark.asyncio
    async def test_storage_counts_survive_worker_empty_cache_after_refresh(
        self, tmp_path
    ) -> None:
        """Storage pending/retry_wait rows + worker cache {} + refresh → snapshot returns storage counts."""
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.core.storage.sqlite.storage import SQLiteStorage

        # Real storage with outbox rows.
        db_path = tmp_path / "test_authority.db"
        storage = SQLiteStorage(str(db_path))
        try:
            await storage.initialize()
            for i in range(5):
                await storage.create_outbox_item(
                    DeliveryOutboxItem(
                        outbox_id=f"obx-auth-{i}",
                        event_id=f"evt-auth-{i}",
                        route_id="route-auth",
                        delivery_plan_id=f"plan-auth-{i}",
                        target_adapter="adapter_auth",
                        attempt_number=1,
                        status="pending",
                    )
                )
            for i in range(3):
                await storage.create_outbox_item(
                    DeliveryOutboxItem(
                        outbox_id=f"obx-retry-{i}",
                        event_id=f"evt-retry-{i}",
                        route_id="route-auth",
                        delivery_plan_id=f"plan-retry-{i}",
                        target_adapter="adapter_auth",
                        attempt_number=2,
                        status="retry_wait",
                    )
                )

            # Build a real MedreApp with storage and a retry worker mock.
            app = _make_medre_app()
            app.storage = storage
            app._retry_worker = MagicMock(outbox_counts={})

            # Before refresh: worker cache {} overwrites storage (expected).
            assert app.outbox_state == {}

            # Refresh from storage.
            await app.refresh_outbox_state_from_storage()

            # After refresh: storage counts are authoritative, worker cache {} ignored.
            counts = app.outbox_state
            assert counts == {"pending": 5, "retry_wait": 3}

            # Flag consumed — subsequent reads resume worker-cache logic.
            counts_after = app.outbox_state
            assert counts_after == {}

            # Full round-trip: refresh → snapshot → storage counts in outbox section.
            await app.refresh_outbox_state_from_storage()
            snap = build_runtime_snapshot(app, snapshot_scope="live")
            assert snap["outbox"]["counts"] == {"pending": 5, "retry_wait": 3}
            assert snap["outbox"]["scope"] == "storage_seeded"
        finally:
            await storage.close()

    def test_worker_empty_cache_without_refresh_is_valid(self) -> None:
        """When no refresh was called, worker cache {} is authoritative.

        This preserves earlier behavior: when the retry worker has completed
        a cycle and found no outbox items, ``{}`` is a valid fresh state and
        should NOT fall back by truthiness to any stale storage counts.
        """
        app = _make_medre_app()
        app._outbox_state = {"pending": 99, "retry_wait": 50}
        app._retry_worker = MagicMock(outbox_counts={})

        # No refresh called — flag is False, worker cache {} is used.
        assert app.outbox_state == {}

    def test_worker_real_counts_without_refresh_are_used(self) -> None:
        """When no refresh was called, worker cache with real counts is used."""
        app = _make_medre_app()
        app._outbox_state = {"pending": 1}
        app._retry_worker = MagicMock(outbox_counts={"pending": 7, "sent": 20})

        assert app.outbox_state == {"pending": 7, "sent": 20}

    @pytest.mark.asyncio
    async def test_refresh_failure_does_not_set_authoritative(self) -> None:
        """If storage refresh fails, the flag is NOT set."""
        app = _make_medre_app()
        mock_storage = MagicMock()
        mock_storage.count_outbox_by_status = AsyncMock(
            side_effect=RuntimeError("db locked")
        )
        app.storage = mock_storage
        app._retry_worker = MagicMock(outbox_counts={})

        await app.refresh_outbox_state_from_storage()
        # Flag should remain False — refresh failed.
        assert app._outbox_storage_authoritative is False
        # Worker cache {} should still be returned.
        assert app.outbox_state == {}
