"""Read-only content accounting across durable ingress and auxiliary copies."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from medre.cli.inspect_commands import _inspect_content
from medre.core.events import EventRelation
from medre.core.storage.content import CONTENT_SURFACES
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
    assert by_table["event_relations"]["logical_bytes"] == len(secret.encode()) + 2
    assert secret not in json.dumps(report)
    assert event.event_id not in json.dumps(report)
    assert await temp_storage.get(event.event_id) == event


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


async def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.sqlite"
    with pytest.raises(SystemExit):
        await _inspect_content(storage_path=str(path))
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
