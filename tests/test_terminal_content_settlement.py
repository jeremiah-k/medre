"""Independent owners and terminal settlement remain durable and fail closed."""

from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _event(event_id="owned"):
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": "private"}
    )


def _outbox(event_id, target):
    return DeliveryOutboxItem(
        outbox_id=f"{event_id}-{target}",
        event_id=event_id,
        delivery_plan_id=f"plan-{target}",
        route_id="route",
        target_adapter="fake_presentation",
        target_channel=target,
    )


async def test_ingress_completion_and_terminal_target_release_are_independent(tmp_path):
    path = str(tmp_path / "settlement.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.admit_ingress(_event(), None, "live")
        a, b = _outbox("owned", "a"), _outbox("owned", "b")
        await store.create_outbox_item(a)
        await store.create_outbox_item(b)
        assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
        assert not await store.complete_ingress_work("owned", worker_id="stale")
        assert (await store.read_event_for_ingress("owned")).status == "retained"
        assert await store.complete_ingress_work("owned", worker_id="worker")
        assert not await store.complete_ingress_work("owned", worker_id="worker")
        assert (await store.read_event_for_ingress("owned")).status == "denied"
        assert (await store.read_event_for_replay("owned")).status == "denied"
        assert (
            await store.read_event_for_outbox("owned", b.outbox_id)
        ).status == "retained"
        assert not await store.mark_outbox_cancelled(
            a.outbox_id, expected_worker_id="stale"
        )
        assert await store.mark_outbox_cancelled(a.outbox_id)
        assert not await store.mark_outbox_cancelled(a.outbox_id)
        assert (
            await store.read_event_for_outbox("owned", a.outbox_id)
        ).status == "denied"
        assert (
            await store.read_event_for_outbox("owned", b.outbox_id)
        ).status == "retained"
        deps = await store.list_mandatory_content_dependencies("owned")
        state = {(kind, owner): released for kind, owner, _, released in deps}
        assert state[("ingress", "owned")] is True
        assert state[("outbox", a.outbox_id)] is True
        assert state[("outbox", b.outbox_id)] is False
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (
            await reopened.read_event_for_outbox("owned", b.outbox_id)
        ).status == "retained"
        assert await reopened.mark_outbox_cancelled(b.outbox_id)
        assert all(
            released
            for _, _, _, released in await reopened.list_mandatory_content_dependencies(
                "owned"
            )
        )
        assert (
            await reopened.read_event_for_outbox("owned", b.outbox_id)
        ).status == "denied"
        assert await reopened.get_body_presence_at_admission("owned") is True
    finally:
        await reopened.close()


async def test_expiry_of_target_hold_cannot_be_undone_by_clock_rollback(tmp_path):
    store = SQLiteStorage(str(tmp_path / "expiry.db"))
    await store.initialize()
    now = datetime.now(timezone.utc)
    try:
        await store.admit_ingress(
            _event(), None, "live", content_deadline=now + timedelta(minutes=5)
        )
        item = _outbox("owned", "target")
        await store.create_outbox_item(item)
        before_expiry = now + timedelta(seconds=1)
        assert (
            await store.read_event_for_outbox(
                "owned", item.outbox_id, now=before_expiry
            )
        ).status == "retained"
        assert (
            await store.read_event_for_outbox(
                "owned", item.outbox_id, now=now + timedelta(minutes=5)
            )
        ).status == "denied"
        assert (
            await store.read_event_for_outbox(
                "owned", item.outbox_id, now=before_expiry
            )
        ).status == "denied"
    finally:
        await store.close()


@pytest.mark.parametrize("owner", ["ingress", "outbox"])
async def test_settlement_failure_rolls_back_lifecycle_and_raises_storage_error(
    tmp_path, owner
):
    store = SQLiteStorage(str(tmp_path / "atomic-failure.db"))
    await store.initialize()
    try:
        await store.admit_ingress(_event(), None, "live")
        item = _outbox("owned", "target")
        await store.create_outbox_item(item)
        await store.claim_ingress_work(worker_id="worker", limit=1)
        await store._write(
            "CREATE TRIGGER refuse_content_settlement "
            "BEFORE UPDATE ON event_content_dependencies "
            "BEGIN SELECT RAISE(ABORT, 'injected settlement failure'); END"
        )
        with pytest.raises(StorageError, match="settlement failure"):
            if owner == "ingress":
                await store.complete_ingress_work("owned", worker_id="worker")
            else:
                await store.mark_outbox_cancelled(item.outbox_id)
        work = await store._read_one(
            "SELECT status, worker_id FROM durable_ingress_work WHERE event_id=?",
            ("owned",),
        )
        assert work["status"] == "processing" and work["worker_id"] == "worker"
        assert (await store.get_outbox_item(item.outbox_id)).status == "pending"
        assert all(
            not released
            for _, _, _, released in await store.list_mandatory_content_dependencies(
                "owned"
            )
        )
        await store._write("DROP TRIGGER refuse_content_settlement")
        if owner == "ingress":
            assert await store.complete_ingress_work("owned", worker_id="worker")
        else:
            assert await store.mark_outbox_cancelled(item.outbox_id)
    finally:
        await store.close()
