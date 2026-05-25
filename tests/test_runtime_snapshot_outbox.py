"""Outbox-specific snapshot tests (split from test_runtime_snapshot.py).

Covers storage-backed outbox counts taking precedence over stale worker
cache, and refresh-outbox-to-storage propagation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from medre.runtime.snapshot import build_runtime_snapshot

from tests.test_runtime_snapshot import _make_fake_app


# ---------------------------------------------------------------------------
# Tests: Outbox storage-backed counts
# ---------------------------------------------------------------------------
class TestOutboxStorageBackedCounts:
    """Outbox counts should reflect storage-seeded data."""

    def test_outbox_precedence_and_structure(self) -> None:
        """Outbox counts reflect storage-backed truth, not worker cache."""
        # 1) No state -> null counts.
        snap_empty = build_runtime_snapshot(_make_fake_app(), snapshot_scope="build")
        assert snap_empty["outbox"]["counts"] is None

        # 2) Storage-seeded counts used when worker cache is empty.
        app = _make_fake_app()
        app._outbox_state = {"pending": 3, "retry_wait": 1}
        app._retry_worker = MagicMock(outbox_counts={})
        snap = build_runtime_snapshot(app, snapshot_scope="build")
        assert snap["outbox"]["counts"] == {"pending": 3, "retry_wait": 1}
        assert snap["outbox"]["scope"] == "storage_seeded"

        # 3) Storage truth wins over stale worker cache.
        app2 = _make_fake_app()
        app2._outbox_state = {"pending": 5, "sent": 10}
        app2._retry_worker = MagicMock(outbox_counts={"pending": 99})
        snap2 = build_runtime_snapshot(app2, snapshot_scope="build")
        assert snap2["outbox"]["counts"] == {"pending": 5, "sent": 10}

        # 4) Worker-only state -> null (worker cache is not authoritative).
        app3 = _make_fake_app()
        app3._retry_worker = MagicMock(outbox_counts={"pending": 7})
        assert build_runtime_snapshot(app3, snapshot_scope="build")["outbox"]["counts"] is None

        # 5) Structure check.
        assert set(snap["outbox"].keys()) == {"counts", "live_refresh", "note", "scope"}
        assert snap["outbox"]["live_refresh"] is False


class TestStorageBackedOutboxRefresh:
    """Regression: outbox rows must appear in snapshot via refresh."""

    @pytest.mark.asyncio
    async def test_refresh_populates_snapshot_from_storage(self, tmp_path) -> None:
        from medre.core.storage.backend import DeliveryOutboxItem
        from medre.core.storage.sqlite import SQLiteStorage

        db_path = tmp_path / "test_refresh.db"
        storage = SQLiteStorage(str(db_path))
        await storage.initialize()
        try:
            for i in range(3):
                await storage.create_outbox_item(DeliveryOutboxItem(
                    outbox_id=f"obx-refresh-{i}", event_id=f"evt-refresh-{i}",
                    route_id="route-refresh", delivery_plan_id=f"plan-refresh-{i}",
                    target_adapter="adapter_refresh", attempt_number=1, status="pending",
                ))
            app = _make_fake_app(storage=storage)
            snap_before = build_runtime_snapshot(app, snapshot_scope="build")
            assert snap_before["outbox"]["counts"] is None
            await app.refresh_outbox_state_from_storage()
            snap_after = build_runtime_snapshot(app, snapshot_scope="build")
            assert snap_after["outbox"]["counts"] == {"pending": 3}
            assert snap_after["outbox"]["scope"] == "storage_seeded"
        finally:
            await storage.close()
