"""Frozen outbox context has exactly one non-conflicting, durable owner."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.backend import OutboxDispatchSnapshotWrite
from medre.core.storage.sqlite._operator_decision import CancelUnstartedDecision
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage_outbox import admit_event, make_outbox_item


async def test_outbox_rejects_conflicting_inline_and_frozen_context(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "conflict.db"))
    await store.initialize()
    try:
        await admit_event(store, "conflicting-context")
        item = replace(
            make_outbox_item(event_id="conflicting-context"),
            metadata={"destination_name": "private route label"},
        )
        with pytest.raises(ValueError, match="one owner"):
            await store.create_outbox_item(
                item,
                dispatch_snapshot=OutboxDispatchSnapshotWrite(
                    kind="direct", context={"destination_name": "other label"}
                ),
            )
        assert await store.get_outbox_item(item.outbox_id) is None
        rows = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots"
        )
        assert rows is not None and rows["n"] == 0
    finally:
        await store.close()


async def test_snapshot_cannot_have_a_competing_creation_generation(
    tmp_path: Path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "unique.db"))
    await store.initialize()
    try:
        await admit_event(store, "one-snapshot")
        item = make_outbox_item(event_id="one-snapshot")
        await store.create_outbox_item(
            item,
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct", context={"capability_level": "native"}
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            await store._run_in_thread(
                lambda: store._require_db().execute(
                    "INSERT INTO delivery_outbox_snapshots "
                    "(outbox_id,attempt_number,snapshot_version,snapshot_kind,context_json,created_at) "
                    "VALUES (?, ?, 1, 'direct', '{}', '2026-10-31')",
                    (item.outbox_id, item.attempt_number + 1),
                )
            )
        rows = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert rows is not None and rows["n"] == 1
    finally:
        await store.close()


async def test_approved_unique_index_upgrade_preserves_existing_work(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "additive.db")
    store = SQLiteStorage(path)
    await store.initialize()
    await admit_event(store, "existing-work")
    item = make_outbox_item(event_id="existing-work")
    await store.create_outbox_item(
        item,
        dispatch_snapshot=OutboxDispatchSnapshotWrite(
            kind="direct", context={"capability_level": "native"}
        ),
    )
    await store.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP INDEX idx_outbox_snapshot_owner_unique")
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        persisted = await reopened.get_outbox_item(item.outbox_id)
        assert persisted is not None and persisted.event_id == "existing-work"
        row = await reopened._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert row is not None and row["n"] == 1
        index = await reopened._read_one(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_outbox_snapshot_owner_unique'"
        )
        assert index is not None
    finally:
        await reopened.close()


async def test_terminal_outbox_cannot_read_unretired_private_snapshot(
    tmp_path: Path,
) -> None:
    """Fail closed even if a malformed terminal row retains a work snapshot."""
    store = SQLiteStorage(str(tmp_path / "terminal.db"))
    await store.initialize()
    try:
        await admit_event(store, "terminal-private")
        item = make_outbox_item(event_id="terminal-private")
        await store.create_outbox_item(
            item,
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct", context={"destination_name": "private"}
            ),
        )
        await store._write(
            "UPDATE delivery_outbox SET status='sent' WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert (
            await store.get_outbox_dispatch_context(
                item.outbox_id,
                event_id=item.event_id,
                delivery_plan_id=item.delivery_plan_id,
                target_adapter=item.target_adapter,
                target_channel=item.target_channel,
            )
            is None
        )
        row = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert row is not None and row["n"] == 1
        assessment = await store.assess_event_content_release(item.event_id)
        assert "unretired_outbox_snapshots" in assessment["blockers"]
    finally:
        await store.close()


async def test_operator_cancellation_retires_snapshot_and_exact_content_hold(
    tmp_path: Path,
) -> None:
    """A terminal operator decision owns snapshot retirement in its own CAS."""
    store = SQLiteStorage(str(tmp_path / "operator.db"))
    await store.initialize()
    try:
        await admit_event(store, "cancel-with-snapshot")
        item = make_outbox_item(event_id="cancel-with-snapshot")
        persisted = await store.create_outbox_item(
            item,
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct", context={"destination_name": "private"}
            ),
        )
        before = await store.get_outbox_item(item.outbox_id)
        assert before is not None
        decision = CancelUnstartedDecision(
            decision_id="cancel-snapshot",
            actor_id="operator",
            identity=DeliveryIdentity(
                before.event_id,
                before.delivery_plan_id,
                before.target_adapter,
                before.target_channel,
            ),
            outbox_id=before.outbox_id,
            expected_generation=before.attempt_number,
            expected_status=before.status,
            expected_updated_at=str(before.updated_at),
        )
        assert persisted.outbox_id == before.outbox_id
        assert await store.cancel_unstarted_delivery(decision) is True
        assert await store.cancel_unstarted_delivery(decision) is False
        after = await store.get_outbox_item(item.outbox_id)
        assert after is not None and after.status == "cancelled"
        snapshot = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert snapshot is not None and snapshot["n"] == 0
        deps = await store.list_mandatory_content_dependencies(item.event_id)
        assert any(
            kind == "outbox" and owner == item.outbox_id and released
            for kind, owner, _, released in deps
        )
        assert len(await store.list_operator_decisions(item.event_id)) == 1
    finally:
        await store.close()


async def test_operator_audit_rollback_retains_snapshot_and_pending_work(
    tmp_path: Path,
) -> None:
    """No partial retirement if the immutable operator audit cannot commit."""
    from medre.core.storage.backend import StorageError

    store = SQLiteStorage(str(tmp_path / "audit-rollback.db"))
    await store.initialize()
    try:
        await admit_event(store, "audit-snapshot")
        item = make_outbox_item(event_id="audit-snapshot")
        before = await store.create_outbox_item(
            item,
            dispatch_snapshot=OutboxDispatchSnapshotWrite(
                kind="direct", context={"destination_name": "private"}
            ),
        )
        decision = CancelUnstartedDecision(
            decision_id="audit-no",
            actor_id="operator",
            identity=DeliveryIdentity(
                before.event_id,
                before.delivery_plan_id,
                before.target_adapter,
                before.target_channel,
            ),
            outbox_id=before.outbox_id,
            expected_generation=before.attempt_number,
            expected_status=before.status,
            expected_updated_at=str(before.updated_at),
        )
        await store._write(
            "CREATE TRIGGER forbid_audit BEFORE INSERT ON operator_delivery_decisions "
            "BEGIN SELECT RAISE(ABORT, 'audit_refused'); END"
        )
        with pytest.raises(StorageError, match="audit_refused"):
            await store.cancel_unstarted_delivery(decision)
        outbox = await store.get_outbox_item(item.outbox_id)
        assert outbox is not None and outbox.status == "pending"
        snapshot = await store._read_one(
            "SELECT COUNT(*) AS n FROM delivery_outbox_snapshots WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert snapshot is not None and snapshot["n"] == 1
        deps = await store.list_mandatory_content_dependencies(item.event_id)
        assert any(
            kind == "outbox" and owner == item.outbox_id and not released
            for kind, owner, _, released in deps
        )
        assert await store.list_operator_decisions(item.event_id) == []
    finally:
        await store.close()
