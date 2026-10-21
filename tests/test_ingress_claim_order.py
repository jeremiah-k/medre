# medre-test-tier: core
"""Fair ingress claims retain eligibility and upgrade existing access paths."""

import sqlite3
from contextlib import closing

import pytest

from medre.core.storage.sqlite.ingress_sql import CLAIM_INGRESS_SELECT
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


@pytest.mark.parametrize(
    "legacy_columns",
    ["status, lease_until, created_at", "status, updated_at, created_at"],
)
async def test_initialize_adds_fair_order_index_to_existing_database(
    tmp_path, legacy_columns: str
) -> None:
    path = str(tmp_path / "medre.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    await storage.close()
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP INDEX idx_ingress_work_ready_order")
        db.execute("DROP INDEX idx_ingress_work_claim")
        db.execute(
            f"CREATE INDEX idx_ingress_work_claim ON durable_ingress_work({legacy_columns})"
        )
        db.commit()
    await storage.initialize()
    try:
        ready = await storage._read_all(
            "PRAGMA index_info(idx_ingress_work_ready_order)"
        )
        assert [row["name"] for row in ready] == [
            "updated_at",
            "created_at",
            "event_id",
        ]
        legacy = await storage._read_all("PRAGMA index_info(idx_ingress_work_claim)")
        assert [row["name"] for row in legacy] == legacy_columns.split(", ")
    finally:
        await storage.close()


@pytest.mark.parametrize("indexed", [True, False])
async def test_claim_eligibility_and_order_with_optional_index(
    temp_storage, indexed
) -> None:
    # Reversed insertion order proves event_id breaks timestamp ties.
    rows = [
        ("pending-b", "pending", None),
        ("pending-a", "pending", None),
        ("expired", "processing", "2000-01-01"),
        ("leased", "processing", "9999-01-01"),
        ("unleased", "processing", None),
        ("completed", "completed", "2000-01-01"),
        ("failed", "failed", "2000-01-01"),
        ("unknown", "invalid", "2000-01-01"),
    ]
    for event_id, status, lease in rows:
        await temp_storage.admit_ingress(
            make_storage_event(event_id=event_id), None, "live"
        )
        await temp_storage._write(
            "UPDATE durable_ingress_work SET status=?, lease_until=?, created_at=?, updated_at=? WHERE event_id=?",
            (status, lease, "2000-01-01", "2000-01-01", event_id),
        )
    if not indexed:
        await temp_storage._write("DROP INDEX idx_ingress_work_ready_order")
    else:
        plan = await temp_storage._read_all(
            "EXPLAIN QUERY PLAN " + CLAIM_INGRESS_SELECT, ("2026-01-01", 10)
        )
        details = " ".join(row["detail"] for row in plan)
        assert "idx_ingress_work_ready_order" in details
        assert "TEMP B-TREE" not in details
    claimed = await temp_storage.claim_ingress_work(worker_id="worker", limit=10)
    assert [item.event_id for item in claimed] == ["expired", "pending-a", "pending-b"]
