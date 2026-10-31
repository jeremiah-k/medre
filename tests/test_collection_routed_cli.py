"""Operator CLI collection batches over routed lifecycle events.

The batch surface itself is unchanged: the same opt-in validation, partial
commit and schema contract apply. These tests pin that a routed event's
lifecycle state — pending targets versus fully settled targets — drives the
per-event outcomes, that refusals preserve committed progress, and that
previews never mutate storage.
"""

from __future__ import annotations

import json
from pathlib import Path

import msgspec

from medre.cli.storage_commands import _storage_collect
from medre.core.events import NativeMessageRef
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_NOW = "2026-10-31T00:00:00+00:00"
_BODY = "private-cli-body"


def _event(event_id):
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": _BODY}, metadata={}
    )


def _inbound_ref(event_id, message_id="native-1"):
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


async def _admit_two_targets(store, event_id):
    await store.admit_ingress(
        _event(event_id), _inbound_ref(event_id, f"native-{event_id}"), "live"
    )
    target_a = _target(event_id, "a", "fake_presentation")
    target_b = _target(event_id, "b", "relay_presentation")
    await store.create_outbox_item(target_a)
    await store.create_outbox_item(target_b)
    assert len(await store.claim_ingress_work(worker_id="worker", limit=1)) == 1
    assert await store.complete_ingress_work(event_id, worker_id="worker")
    return target_a, target_b


async def _settle_all(store, target_a, target_b):
    # Terminal-from-pending settlement: no claim, fully deterministic, and it
    # leaves every other event's live work untouched.
    assert await store.mark_outbox_cancelled(target_a.outbox_id)
    assert await store.mark_outbox_cancelled(target_b.outbox_id)


def _validate_batch(report):
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = Path(__file__).resolve().parents[1] / "docs/schemas"
    batch = json.loads((schemas / "content-collection-batch.schema.json").read_text())
    event = json.loads((schemas / "event-content-collection.schema.json").read_text())
    Draft202012Validator.check_schema(batch)
    Draft202012Validator.check_schema(event)
    registry = Registry().with_resource(event["$id"], Resource.from_contents(event))
    Draft202012Validator(batch, registry=registry).validate(report)


async def test_cli_batch_refuses_pending_target_and_preserves_progress(
    tmp_path, capsys
):
    path = str(tmp_path / "partial.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        # Lexicographic page order: the refused event sorts first, so the
        # later settled event proves committed progress is preserved.
        pending_a, pending_b = await _admit_two_targets(store, "a-pending-event")
        settled_a, settled_b = await _admit_two_targets(store, "z-settled-event")
        await _settle_all(store, settled_a, settled_b)
        stored_pending = await store.get_outbox_item(pending_b.outbox_id)
        assert stored_pending is not None and stored_pending.status == "pending"
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    applied = json.loads(capsys.readouterr().out)
    _validate_batch(applied)
    # A live target yields a content-free blocked report in place; the later
    # settled event still commits, proving preserved progress.
    assert [result["status"] for result in applied["results"]] == [
        "blocked",
        "collected",
    ]
    assert "active_outbox" in applied["results"][0]["blockers"]
    assert applied["results"][0]["candidate_rows"] == 1
    assert applied["events_refused"] == 0
    assert applied["rows_removed"] == 1
    assert applied["next_after"] == "z-settled-event"
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        row = await reopened._read_one(
            "SELECT body FROM event_body_content WHERE event_id='a-pending-event'"
        )
        assert row["body"] == _BODY
        assert (
            await reopened._read_one(
                "SELECT COUNT(*) AS n FROM event_body_content "
                "WHERE event_id='z-settled-event'"
            )
            is not None
        )
        gone = await reopened._read_one(
            "SELECT COUNT(*) AS n FROM event_body_content "
            "WHERE event_id='z-settled-event'"
        )
        assert int(gone["n"]) == 0
    finally:
        await reopened.close()


async def test_cli_batch_collects_after_settlement_end_to_end(tmp_path, capsys):
    path = str(tmp_path / "end-to-end.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store, "routed")
        await _settle_all(store, target_a, target_b)
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    applied = json.loads(capsys.readouterr().out)
    _validate_batch(applied)
    assert applied["results"][0]["status"] == "collected"
    assert applied["results"][0]["rows_removed"] == 1
    assert applied["logical_bytes_removed"] == len(_BODY)
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    again = json.loads(capsys.readouterr().out)
    _validate_batch(again)
    assert again["results"][0]["status"] == "already_collected"
    await _storage_collect(path, mode="retain")
    retained = json.loads(capsys.readouterr().out)
    _validate_batch(retained)
    assert retained["mode"] == "retain"
    assert retained["results"][0]["status"] == "retained_by_policy"


async def test_cli_preview_reports_blockers_without_mutating(tmp_path, capsys):
    path = str(tmp_path / "preview.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        target_a, target_b = await _admit_two_targets(store, "routed")
        claimed = await store.claim_due_outbox_items(
            now=_NOW, worker_id="worker", excluded_adapters=("relay_presentation",)
        )
        assert [item.outbox_id for item in claimed] == [target_a.outbox_id]
        assert await store.mark_outbox_sent(target_a.outbox_id)
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only")
    preview = json.loads(capsys.readouterr().out)
    _validate_batch(preview)
    assert preview["dry_run"] is True
    assert preview["results"][0]["status"] == "blocked"
    assert "active_outbox" in preview["results"][0]["blockers"]
    assert preview["rows_removed"] == 0
    assert preview["logical_bytes_removed"] == 0
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        row = await reopened._read_one(
            "SELECT body FROM event_body_content WHERE event_id='routed'"
        )
        assert row["body"] == _BODY
        none_yet = await reopened._read_one(
            "SELECT COUNT(*) AS n FROM event_content_collection_log"
        )
        assert int(none_yet["n"]) == 0
    finally:
        await reopened.close()
