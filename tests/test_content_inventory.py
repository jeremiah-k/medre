"""Read-only content accounting across durable ingress and auxiliary copies."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import msgspec
import pytest

from medre.cli.inspect_commands import _inspect_content
from medre.core.events import EventRelation, NativeMessageRef
from medre.core.storage.content import CONTENT_SURFACES, inventory_sql
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_empty_inventory_includes_all_surfaces(temp_storage):
    report = await temp_storage.inspect_content_inventory()
    assert {item["table"] for item in report["surfaces"]} == {
        surface.table for surface in CONTENT_SURFACES
    }
    assert all(
        item["rows"] == item["logical_bytes"] == 0 for item in report["surfaces"]
    )
    assert report["retention_authorized"] is False
    schema = json.loads(Path("docs/schemas/content-inventory.schema.json").read_text())
    jsonschema.validate(report, schema)


async def test_unicode_and_relation_copies_count_without_returning_values(temp_storage):
    secret = "secret quoted reply ☃"
    event = make_storage_event(
        payload={"body": secret},
        relations=(
            EventRelation(
                relation_type="reply",
                target_event_id=None,
                target_native_ref=None,
                key=None,
                fallback_text=secret,
            ),
        ),
    )
    await temp_storage.append(event)
    report = await temp_storage.inspect_content_inventory()
    by_table = {item["table"]: item for item in report["surfaces"]}
    assert by_table["canonical_events"]["rows"] == 1
    assert by_table["event_relations"]["rows"] == 1
    # Relation identity keeps only its metadata envelope; the reply excerpt
    # and the canonical body now live in their own content tables.
    assert by_table["event_relations"]["logical_bytes"] == 2
    assert by_table["event_relation_excerpts"]["rows"] == 1
    assert by_table["event_relation_excerpts"]["logical_bytes"] == len(secret.encode())
    assert by_table["event_body_content"]["rows"] == 1
    assert by_table["event_body_content"]["logical_bytes"] == len(secret.encode())
    assert secret not in json.dumps(report)
    assert event.event_id not in json.dumps(report)
    # Ordinary get() is metadata-only: identity survives, but neither the
    # body nor the relation excerpt is hydrated without an owner-bound read.
    public = await temp_storage.get(event.event_id)
    assert public is not None
    assert public.event_id == event.event_id
    assert public.payload == {}
    assert secret not in msgspec.json.encode(public).decode("utf-8")


async def test_attachment_bytes_count_independently_of_event_payload(temp_storage):
    await temp_storage._write(
        "INSERT INTO attachment_blobs(content_ref, size_bytes, created_at, data) VALUES (?, ?, ?, ?)",
        ("private-blob-ref", 4, "2026-10-28T00:00:00+00:00", b"abcd"),
    )
    report = await temp_storage.inspect_content_inventory()
    blob = next(
        item for item in report["surfaces"] if item["table"] == "attachment_blobs"
    )
    assert (blob["rows"], blob["logical_bytes"]) == (1, 4)
    assert "private-blob-ref" not in json.dumps(report)


async def test_cli_opens_readonly_and_preserves_content(tmp_path, capsys):
    path = tmp_path / "private.sqlite"
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    event = make_storage_event()
    await storage.append(event)
    await storage.close()
    before = path.read_bytes()
    await _inspect_content(storage_path=str(path))
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "core_sqlite"
    assert str(path) not in json.dumps(report)
    assert path.read_bytes() == before


async def test_missing_database_refuses_path_free_without_creating(tmp_path, capsys):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(SystemExit) as refusal:
        await _inspect_content(storage_path=str(path))
    assert refusal.value.code == 2
    output = capsys.readouterr()
    assert "incomplete or unreadable" in output.err
    assert str(path) not in output.err
    assert not path.exists()


async def test_absent_content_table_refuses_without_creating_storage(tmp_path, capsys):
    path = tmp_path / "incomplete.sqlite"
    storage = SQLiteStorage(str(path))
    await storage.initialize()
    await storage._write("DROP TABLE attachment_blobs")
    await storage.close()
    before = path.read_bytes()
    with pytest.raises(SystemExit) as refusal:
        await _inspect_content(storage_path=str(path))
    assert refusal.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "incomplete or unreadable" in output.err
    assert str(path) not in output.err
    assert path.read_bytes() == before


async def test_absent_content_column_refuses_instead_of_counting_literals(
    tmp_path, capsys
):
    storage = SQLiteStorage(str(tmp_path / "incomplete-column.sqlite"))
    await storage.initialize()
    await storage._write("ALTER TABLE attachment_blobs RENAME COLUMN data TO renamed")
    await storage.close()
    with pytest.raises(SystemExit) as refusal:
        await _inspect_content(storage_path=str(tmp_path / "incomplete-column.sqlite"))
    assert refusal.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "incomplete or unreadable" in output.err


async def test_inventory_associates_rows_by_surface_not_result_order(
    temp_storage, monkeypatch
):
    await temp_storage.append(make_storage_event())
    original_sql = inventory_sql()

    def reversed_sql():
        return " UNION ALL ".join(reversed(original_sql.split(" UNION ALL ")))

    monkeypatch.setattr("medre.core.storage.sqlite._count.inventory_sql", reversed_sql)
    report = await temp_storage.inspect_content_inventory()
    by_table = {item["table"]: item for item in report["surfaces"]}
    assert by_table["canonical_events"]["rows"] == 1
    assert by_table["delivery_outbox"]["rows"] == 0
    assert by_table["attachment_blobs"]["logical_bytes"] == 0


async def test_native_and_evidence_copies_count_without_returning_values(
    temp_storage,
):
    quoted = "retained native quote"
    await temp_storage.append(make_storage_event(event_id="evt-copy"))
    await temp_storage.store_native_ref(
        NativeMessageRef(
            id="nref-copy",
            event_id="evt-copy",
            adapter="matrix-main",
            native_channel_id="!room:example.org",
            native_message_id="$1",
            native_thread_id=None,
            native_relation_id=None,
            direction="outbound",
            metadata={"quote": quoted},
        )
    )
    await temp_storage._write(
        "INSERT INTO delivery_receipts "
        "(receipt_id, event_id, delivery_plan_id, target_adapter, status, "
        "receipt_kind, rendering_evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "rcpt-copy",
            "evt-copy",
            "plan-copy",
            "matrix-main",
            "sent",
            "attempt",
            '{"quote": "retained native quote"}',
            "2026-10-30T00:00:00+00:00",
        ),
    )
    report = await temp_storage.inspect_content_inventory()
    by_table = {item["table"]: item for item in report["surfaces"]}
    refs = by_table["native_message_refs"]
    assert refs["rows"] == 1
    # Native metadata is stored as compact JSON by the storage layer.
    assert refs["logical_bytes"] == len('{"quote":"retained native quote"}'.encode())
    receipts = by_table["delivery_receipts"]
    assert receipts["rows"] == 1
    assert receipts["logical_bytes"] == len(
        '{"quote": "retained native quote"}'.encode()
    )
    assert quoted not in json.dumps(report)
