"""Atomic, event-scoped cancellation cannot touch a native submission."""

from __future__ import annotations

from dataclasses import replace

import pytest

from medre.core.delivery_authority import DeliveryIdentity
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite._operator_decision import (
    CancelUnstartedDecision,
    OperatorDecisionRefused,
)


async def _seed(storage, event, channel, *, outbox_id):
    await storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id=outbox_id,
            event_id=event.event_id,
            delivery_plan_id="plan",
            target_adapter="matrix",
            target_channel=channel,
            route_id="route",
            status="pending",
        )
    )
    result = await storage.get_outbox_item(outbox_id)
    assert result is not None
    return result


def _decision(item, *, decision_id="decision-1", channel=None):
    return CancelUnstartedDecision(
        decision_id=decision_id,
        actor_id="operator-test",
        identity=DeliveryIdentity(
            item.event_id,
            item.delivery_plan_id,
            item.target_adapter,
            item.target_channel if channel is None else channel,
        ),
        outbox_id=item.outbox_id,
        expected_generation=item.attempt_number,
        expected_status=item.status,
        expected_updated_at=(
            item.updated_at.isoformat()
            if hasattr(item.updated_at, "isoformat")
            else str(item.updated_at)
        ),
    )


async def test_cancel_one_room_without_touching_sibling_or_reopening(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    left = await _seed(temp_storage, sample_event, "!left", outbox_id="obox-left")
    await _seed(temp_storage, sample_event, "!right", outbox_id="obox-right")
    requested = _decision(left)
    assert await temp_storage.cancel_unstarted_delivery(requested) is True
    assert await temp_storage.cancel_unstarted_delivery(requested) is False
    assert (await temp_storage.get_outbox_item("obox-left")).status == "cancelled"
    assert (await temp_storage.get_outbox_item("obox-right")).status == "pending"
    # The guarded cancellation settles exactly the cancelled target owner;
    # the sibling keeps its independently valid hold.
    deps = await temp_storage.list_mandatory_content_dependencies(sample_event.event_id)
    state = {(kind, owner): released for kind, owner, _, released in deps}
    assert state[("outbox", "obox-left")] is True
    assert state[("outbox", "obox-right")] is False
    evidence = await temp_storage.list_operator_decisions(sample_event.event_id)
    assert len(evidence) == 1
    assert evidence[0]["actor_id"] == "operator-test"
    assert "message" not in str(evidence)
    with pytest.raises(OperatorDecisionRefused, match="already binds"):
        await temp_storage.cancel_unstarted_delivery(
            replace(requested, actor_id="other")
        )


async def test_claimed_and_reserved_work_refuses_exact_cancellation(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, None, outbox_id="obox-claimed")
    requested = _decision(item)
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    with pytest.raises(OperatorDecisionRefused):
        await temp_storage.cancel_unstarted_delivery(requested)
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "in_progress"
    # Even if the worker reserves after the operator's snapshot, its native
    # submission boundary must remain intact and cannot be erased by cancel.
    assert (
        await temp_storage.reserve_outbox_attempt(
            item.outbox_id, "retry", item.attempt_number
        )
        == item.attempt_number + 1
    )
    with pytest.raises(OperatorDecisionRefused):
        await temp_storage.cancel_unstarted_delivery(requested)
    assert (
        await temp_storage.get_outbox_item(item.outbox_id)
    ).active_attempt == item.attempt_number + 1
    assert await temp_storage.list_operator_decisions(sample_event.event_id) == []


async def test_wrong_channel_and_stale_version_refuse_without_audit(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, "!first", outbox_id="obox-fenced")
    with pytest.raises(OperatorDecisionRefused, match="no longer exists"):
        await temp_storage.cancel_unstarted_delivery(_decision(item, channel="!other"))
    with pytest.raises(OperatorDecisionRefused, match="Stale"):
        await temp_storage.cancel_unstarted_delivery(
            replace(_decision(item), expected_updated_at="2000-01-01T00:00:00+00:00")
        )
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await temp_storage.list_operator_decisions(sample_event.event_id) == []


async def test_reopen_preserves_decision_and_fence(tmp_path, sample_event):
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "operational.sqlite")
    first = SQLiteStorage(path)
    await first.initialize()
    await first.append(sample_event)
    item = await _seed(first, sample_event, "!room", outbox_id="obox-reopen")
    requested = _decision(item)
    assert await first.cancel_unstarted_delivery(requested)
    await first.close()

    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert await reopened.cancel_unstarted_delivery(requested) is False
        assert (await reopened.get_outbox_item(item.outbox_id)).status == "cancelled"
        assert len(await reopened.list_operator_decisions(sample_event.event_id)) == 1
        assert (
            await reopened.claim_due_outbox_items(
                now="2030-01-01T00:00:00",
                worker_id="retry",
                limit=1,
            )
            == []
        )
    finally:
        await reopened.close()


async def test_old_prerelease_schema_refused_without_silent_upgrade(tmp_path):
    import sqlite3

    from medre.core.storage.backend import PreReleaseSchemaMismatchError
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "old.sqlite")
    original = SQLiteStorage(path)
    await original.initialize()
    await original.close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE operator_delivery_decisions")
    db.commit()
    db.close()
    restored = SQLiteStorage(path)
    with pytest.raises(PreReleaseSchemaMismatchError):
        await restored.initialize()
    check = sqlite3.connect(path)
    try:
        assert (
            check.execute(
                "SELECT 1 FROM sqlite_master WHERE name='operator_delivery_decisions'"
            ).fetchone()
            is None
        )
    finally:
        check.close()


async def test_competing_storage_owner_cannot_claim_cancelled_leg(
    tmp_path, sample_event
):
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "competing.sqlite")
    first, second = SQLiteStorage(path), SQLiteStorage(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.append(sample_event)
        item = await _seed(first, sample_event, "!room", outbox_id="obox-race")
        decision = _decision(item)
        assert await first.cancel_unstarted_delivery(decision)
        assert (
            await second.claim_due_outbox_items(
                now="2035-01-01T00:00:00",
                worker_id="other-worker",
                limit=1,
            )
            == []
        )
        assert (await second.get_outbox_item(item.outbox_id)).status == "cancelled"
    finally:
        await first.close()
        await second.close()


async def test_unreserved_deferred_attempt_can_be_cancelled_without_native_send(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, "!retry", outbox_id="obox-defer")
    await temp_storage.claim_due_outbox_items(
        now="2026-12-01T00:00:00", worker_id="retry", limit=1
    )
    assert await temp_storage.mark_outbox_retry_wait(
        item.outbox_id, "2030-01-01T00:00:00", expected_worker_id="retry"
    )
    deferred = await temp_storage.get_outbox_item(item.outbox_id)
    assert deferred.status == "retry_wait"
    assert deferred.active_attempt is None
    assert await temp_storage.cancel_unstarted_delivery(
        _decision(deferred, decision_id="cancel-deferred")
    )
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "cancelled"


async def test_competing_reserved_generation_refuses_cancellation(
    temp_storage, sample_event
):
    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="pending")
    # A valid persisted sibling can have a lower finalized counter while its
    # reserved generation equals the selected pending generation.
    await temp_storage._write(
        "INSERT INTO delivery_outbox (outbox_id,event_id,delivery_plan_id,"
        "target_adapter,target_channel,attempt_number,active_attempt,status,"
        "created_at,updated_at,worker_id) VALUES (?,?,?,?,?,0,1,'in_progress',?,?,?)",
        (
            "reserved",
            sample_event.event_id,
            "plan",
            "matrix",
            "!room",
            item.created_at,
            item.updated_at,
            "owner",
        ),
    )
    with pytest.raises(OperatorDecisionRefused, match="competing"):
        await temp_storage.cancel_unstarted_delivery(_decision(item))
    assert (await temp_storage.get_outbox_item("pending")).status == "pending"
    assert (await temp_storage.get_outbox_item("reserved")).active_attempt == 1
    assert await temp_storage.list_operator_decisions(sample_event.event_id) == []


async def test_audit_failure_rolls_back_cancellation(temp_storage, sample_event):
    from medre.core.storage.backend import StorageError

    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="audit-failure")
    await temp_storage._write(
        "CREATE TRIGGER refuse_operator_audit BEFORE INSERT ON operator_delivery_decisions "
        "BEGIN SELECT RAISE(ABORT, 'audit_unavailable'); END"
    )
    with pytest.raises(StorageError, match="audit_unavailable"):
        await temp_storage.cancel_unstarted_delivery(_decision(item))
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await temp_storage.list_operator_decisions(sample_event.event_id) == []


@pytest.mark.parametrize("missing", ["primary_key", "status_check"])
async def test_audit_shape_requires_idempotency_and_status_constraints(
    tmp_path, missing
):
    import sqlite3
    from contextlib import closing

    from medre.core.storage.backend import PreReleaseSchemaConstraintMismatchError
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "invalid-audit.sqlite")
    original = SQLiteStorage(path)
    await original.initialize()
    await original.close()
    with closing(sqlite3.connect(path)) as db:
        sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='operator_delivery_decisions'"
        ).fetchone()[0]
        if missing == "primary_key":
            sql = sql.replace("decision_id TEXT PRIMARY KEY", "decision_id TEXT")
        else:
            sql = sql.replace(
                "CHECK (observed_status IN ('pending', 'retry_wait')),", ""
            )
        db.execute("DROP TABLE operator_delivery_decisions")
        db.execute(sql)
        db.commit()
    reopened = SQLiteStorage(path)
    try:
        with pytest.raises(PreReleaseSchemaConstraintMismatchError):
            await reopened.initialize()
    finally:
        await reopened.close()


async def test_competing_claim_and_cancel_have_only_one_winner(tmp_path, sample_event):
    import asyncio

    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = str(tmp_path / "claim-race.sqlite")
    left, right = SQLiteStorage(path), SQLiteStorage(path)
    await left.initialize()
    await right.initialize()
    try:
        await left.append(sample_event)
        item = await _seed(left, sample_event, "!room", outbox_id="race")
        cancelled, claimed = await asyncio.gather(
            left.cancel_unstarted_delivery(_decision(item)),
            right.claim_due_outbox_items(
                now="2035-01-01T00:00:00", worker_id="worker", limit=1
            ),
            return_exceptions=True,
        )
        result = await left.get_outbox_item(item.outbox_id)
        if cancelled is True:
            assert claimed == []
            assert result.status == "cancelled"
            assert len(await left.list_operator_decisions(sample_event.event_id)) == 1
        else:
            assert isinstance(cancelled, OperatorDecisionRefused)
            assert isinstance(claimed, list) and len(claimed) == 1
            assert result.status == "in_progress"
            assert await left.list_operator_decisions(sample_event.event_id) == []
    finally:
        await left.close()
        await right.close()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE delivery_outbox SET worker_id='owner' WHERE outbox_id=?",
        "UPDATE delivery_outbox SET locked_at=updated_at WHERE outbox_id=?",
        "UPDATE delivery_outbox SET lease_until=updated_at WHERE outbox_id=?",
    ],
)
async def test_pending_row_with_retained_ownership_refuses_cancellation(
    temp_storage, sample_event, statement
):
    await temp_storage.append(sample_event)
    item = await _seed(temp_storage, sample_event, "!room", outbox_id="retained-claim")
    requested = _decision(item)
    await temp_storage._write(statement, (item.outbox_id,))
    with pytest.raises(
        OperatorDecisionRefused, match="Stale generation or active attempt"
    ):
        await temp_storage.cancel_unstarted_delivery(requested)
    assert (await temp_storage.get_outbox_item(item.outbox_id)).status == "pending"
    assert await temp_storage.list_operator_decisions(sample_event.event_id) == []
