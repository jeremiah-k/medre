"""Collection invariants that outlive the expanded collectable event set.

A collected routed event is never eligible for an inferred resend, the
collection receipt creates no dispatch or replay bookkeeping, and the
out-of-scope copy families — radio plans and attachment associations —
keep blocking under their independent policies.
"""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec

from medre.core.events import NativeMessageRef
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_NOW = "2026-10-31T00:00:00+00:00"
_BODY = "private-invariant-body"


def _event(event_id="routed"):
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": _BODY}, metadata={}
    )


def _inbound_ref(event_id, message_id="native-routed"):
    return NativeMessageRef(
        id=f"nref-{event_id}",
        event_id=event_id,
        adapter="fake_transport",
        native_channel_id="ch-0",
        native_message_id=message_id,
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
    )


def _target(event_id, name, adapter):
    return DeliveryOutboxItem(
        outbox_id=f"obox-{event_id}-{name}",
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id=f"plan-{name}",
        target_adapter=adapter,
        target_channel=name,
    )


async def _settle_routed(store, event_id="routed"):
    """Admit through durable ingress and settle every target terminally."""
    await store.admit_ingress(_event(event_id), _inbound_ref(event_id), "live")
    target_a = _target(event_id, "a", "fake_presentation")
    target_b = _target(event_id, "b", "relay_presentation")
    await store.create_outbox_item(target_a)
    await store.create_outbox_item(target_b)
    assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
    assert await store.complete_ingress_work(event_id, worker_id="worker")
    await store.claim_due_outbox_items(now=_NOW, worker_id="worker")
    assert await store.mark_outbox_sent(target_a.outbox_id)
    assert await store.mark_outbox_dead_lettered(
        target_b.outbox_id, failure_kind="adapter_permanent"
    )
    return target_a, target_b


async def _count(store, sql):
    row = await store._read_one(sql)
    return int(row["n"])


async def test_collected_event_cannot_authorize_native_submission(tmp_path):
    store = SQLiteStorage(str(tmp_path / "fence.db"))
    await store.initialize()
    try:
        target_b = _target("routed", "b", "relay_presentation")
        target_a = _target("routed", "a", "fake_presentation")
        await store.admit_ingress(_event(), _inbound_ref("routed"), "live")
        await store.create_outbox_item(target_a)
        await store.create_outbox_item(target_b)
        assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
        assert await store.complete_ingress_work("routed", worker_id="worker")
        # The claim's lease and the dependency's acquisition share the real
        # clock, so the reservation is live against the default observation.
        claimed = await store.claim_due_outbox_items(
            now=datetime.now(timezone.utc).isoformat(),
            worker_id="worker",
            excluded_adapters=("fake_presentation",),
        )
        assert [item.outbox_id for item in claimed] == [target_b.outbox_id]
        reserved = await store.reserve_outbox_attempt(
            target_b.outbox_id, "worker", from_attempt=1
        )
        assert reserved == 2
        # The live, reserved worker may submit natively while content is held.
        assert await store.authorize_native_submission(
            "routed",
            target_b.outbox_id,
            worker_id="worker",
            attempt_number=reserved,
        )
        assert await store.mark_outbox_dead_lettered(
            target_b.outbox_id, failure_kind="adapter_permanent"
        )
        assert await store.mark_outbox_cancelled(target_a.outbox_id)
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        # After collection the fence stays closed: a collected event is never
        # eligible for an inferred resend, under any worker identity.
        assert not await store.authorize_native_submission(
            "routed",
            target_b.outbox_id,
            worker_id="worker",
            attempt_number=reserved,
        )
        assert not await store.authorize_native_submission(
            "routed",
            target_b.outbox_id,
            worker_id="worker",
            attempt_number=1,
        )
    finally:
        await store.close()


async def test_collection_receipt_does_not_create_replay_or_outbox_rows(tmp_path):
    store = SQLiteStorage(str(tmp_path / "bookkeeping.db"))
    await store.initialize()
    try:
        target_a, target_b = await _settle_routed(store)
        outbox_rows = await _count(
            store, "SELECT COUNT(*) AS n FROM delivery_outbox WHERE event_id='routed'"
        )
        replay_runs = await store.count_replay_runs()
        assert outbox_rows == 2
        assert replay_runs == 0
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
        assert (
            await _count(
                store, "SELECT COUNT(*) AS n FROM event_content_collection_log"
            )
            == 1
        )
        # Nothing in the receipt write opened dispatch or replay bookkeeping.
        assert (
            await _count(
                store,
                "SELECT COUNT(*) AS n FROM delivery_outbox WHERE event_id='routed'",
            )
            == outbox_rows
        )
        assert await store.count_replay_runs() == replay_runs
        items = await store.list_outbox_items_for_event("routed")
        assert {item.outbox_id for item in items} == {
            target_a.outbox_id,
            target_b.outbox_id,
        }
        assert all(item.status in {"sent", "dead_lettered"} for item in items)
    finally:
        await store.close()


async def test_radio_and_shared_attachment_surfaces_still_block(tmp_path):
    """Radio plans and attachment associations keep their wholesale policy."""
    store = SQLiteStorage(str(tmp_path / "scope.db"))
    await store.initialize()
    try:
        await _settle_routed(store)
        fingerprint = "a" * 64
        await store._write(
            "INSERT INTO radio_attempt_progress "
            "(outbox_id,attempt_number,plan_fingerprint,plan_json,provenance_json,"
            "parts_json,revision,created_at,updated_at) "
            "SELECT outbox_id, 1, ?, '{}', '{}', '{}', 0, "
            "'2026-10-31T00:00:00+00:00', '2026-10-31T00:00:00+00:00' "
            "FROM delivery_outbox WHERE event_id='routed' AND target_adapter="
            "'relay_presentation'",
            (fingerprint,),
        )
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert "radio_plans" in blocked["blockers"]
        await store._write("DELETE FROM radio_attempt_progress")
        # A shared attachment copy keeps blocking through the association.
        await store.append(
            msgspec.structs.replace(
                make_storage_event(event_id="other"),
                payload={},
                metadata={},
            )
        )
        shared_ref = "sha256:" + "b" * 64
        await store._write(
            "INSERT INTO attachment_blobs "
            "(content_ref,size_bytes,media_kind,mime_type,created_at,data) "
            "VALUES (?, 3, NULL, NULL, '2026-10-31T00:00:00+00:00', ?)",
            (shared_ref, b"abc"),
        )
        await store._write(
            "INSERT INTO event_attachment_associations "
            "(event_id,content_ref,created_at) VALUES "
            "('routed', ?, '2026-10-31T00:00:00+00:00'), "
            "('other', ?, '2026-10-31T00:00:00+00:00')",
            (shared_ref, shared_ref),
        )
        assessment = await store.assess_event_content_release("routed")
        copies = {copy["surface"]: copy["rows"] for copy in assessment["copies"]}
        assert copies["attachment_associations"] == 1
        assert copies["shared_attachment_references"] == 1
        blocked = await store.collect_event_content("routed", dry_run=False)
        assert blocked["status"] == "blocked"
        assert blocked["blockers"] == ["attachment_associations"]
        await store._write("DELETE FROM event_attachment_associations")
        await store._write("DELETE FROM attachment_blobs")
        assert (await store.collect_event_content("routed", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()
