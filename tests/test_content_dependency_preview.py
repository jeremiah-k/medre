"""Read-only per-event dependency projection never returns message content."""

import json

import pytest

from medre.cli.inspect_commands import _inspect_content
from medre.core.storage.backend import StorageError
from tests.helpers.storage import make_storage_event


async def test_dependency_preview_is_read_only_and_omits_message_bytes(temp_storage):
    event = make_storage_event(payload={"body": "PRIVATE-EXCERPT-UNIQUE"})
    await temp_storage.append(event)
    report = await temp_storage.inspect_event_content_dependencies(event.event_id)
    assert report["retention_authorized"] is False
    assert report["deletion_authorized"] is False
    assert report["delivery_hold_candidate"] is False
    assert report["content_copies"]["relations"] == 0
    assert "PRIVATE-EXCERPT-UNIQUE" not in json.dumps(report)
    from pathlib import Path

    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).parents[1]
        / "docs/schemas/content-dependency-preview.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    Draft202012Validator(schema).validate(report)
    assert "provider_native_queues_and_histories" in report["unmeasured"]


async def test_missing_dependency_event_fails_closed(temp_storage):
    with pytest.raises(StorageError, match="does not exist"):
        await temp_storage.inspect_event_content_dependencies("unknown-event")
    with pytest.raises(ValueError):
        await temp_storage.inspect_event_content_dependencies("")


async def test_cli_content_event_preview_on_readonly_reopened_sqlite(tmp_path, capsys):
    from medre.core.storage.sqlite.storage import SQLiteStorage

    path = tmp_path / "db.sqlite"
    store = SQLiteStorage(str(path))
    await store.initialize()
    event = make_storage_event(payload={"body": "DO-NOT-PRINT"})
    await store.append(event)
    await store.close()
    before = path.read_bytes()
    await _inspect_content(storage_path=str(path), event_id=event.event_id)
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "core_sqlite_event"
    assert result["event_id"] == event.event_id
    assert "DO-NOT-PRINT" not in json.dumps(result)
    assert path.read_bytes() == before


async def test_processing_and_failed_ingress_keep_a_hold_candidate(temp_storage):
    event = make_storage_event()
    await temp_storage.admit_ingress(event, None, "live")
    [work] = await temp_storage.claim_ingress_work(worker_id="owner", limit=1)
    assert work.status == "processing"
    report = await temp_storage.inspect_event_content_dependencies(event.event_id)
    assert report["delivery_hold_candidate"] is True
    await temp_storage.fail_ingress_work(
        event.event_id, worker_id="owner", error="failed"
    )
    report = await temp_storage.inspect_event_content_dependencies(event.event_id)
    assert report["delivery_hold_candidate"] is True


@pytest.mark.parametrize(
    "status, hold",
    [
        ("pending", True),
        ("processing", True),
        ("failed", True),
        ("completed", False),
        ("future_state", True),
    ],
)
async def test_ingress_hold_candidates_fail_conservatively(temp_storage, status, hold):
    event = make_storage_event()
    await temp_storage.admit_ingress(event, None, "live")
    await temp_storage._write(
        "UPDATE durable_ingress_work SET status=? WHERE event_id=?",
        (status, event.event_id),
    )
    report = await temp_storage.inspect_event_content_dependencies(event.event_id)
    assert report["ingress"] == [{"status": status, "count": 1}]
    assert report["delivery_hold_candidate"] is hold
    assert report["deletion_authorized"] is False


async def test_reply_dependencies_count_both_directions_without_excerpts(temp_storage):
    from medre.core.events.canonical import EventRelation

    parent = make_storage_event(event_id="parent")
    reply = make_storage_event(event_id="reply")
    await temp_storage.append(parent)
    await temp_storage.append(reply)
    await temp_storage.store_relation(
        reply.event_id,
        EventRelation("reply", parent.event_id, None, None, "PRIVATE-QUOTE"),
    )
    parent_report = await temp_storage.inspect_event_content_dependencies(
        parent.event_id
    )
    reply_report = await temp_storage.inspect_event_content_dependencies(reply.event_id)
    assert parent_report["content_copies"]["incoming_relations"] == 1
    assert parent_report["content_copies"]["relations"] == 0
    assert reply_report["content_copies"]["relations"] == 1
    assert reply_report["content_copies"]["incoming_relations"] == 0
    assert "PRIVATE-QUOTE" not in json.dumps([parent_report, reply_report])


@pytest.mark.parametrize(
    "status, hold",
    [
        ("pending", True),
        ("in_progress", True),
        ("queued", True),
        ("retry_wait", True),
        ("abandoned", True),
        ("dead_lettered", True),
        ("sent", False),
        ("cancelled", False),
    ],
)
async def test_outbox_hold_candidates_preserve_unsettled_work(
    temp_storage, status, hold
):
    from medre.core.storage.backend import DeliveryOutboxItem

    event = make_storage_event()
    await temp_storage.append(event)
    await temp_storage.create_outbox_item(
        DeliveryOutboxItem(
            outbox_id="box",
            event_id=event.event_id,
            delivery_plan_id="plan",
            route_id="route",
            target_adapter="matrix",
            target_channel="!room",
            status="pending",
        )
    )
    # Exercise retained and unknown states without fabricating lifecycle receipts.
    await temp_storage._write(
        "UPDATE delivery_outbox SET status=? WHERE outbox_id=?", (status, "box")
    )
    report = await temp_storage.inspect_event_content_dependencies(event.event_id)
    assert report["outbox"] == [{"status": status, "count": 1}]
    assert report["delivery_hold_candidate"] is hold
    assert report["deletion_authorized"] is False
