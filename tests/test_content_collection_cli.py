"""Operator controls for bounded, non-scheduled logical collection."""

import json

import msgspec
import pytest

from medre.cli.storage_commands import _storage_collect
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_collection_cli_defaults_to_retain_and_requires_explicit_opt_in(
    tmp_path, capsys
):
    path = str(tmp_path / "operator.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="secret"),
            payload={"body": "top-secret"},
            metadata={},
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id='secret' AND owner_kind='append'",
            ("2026-10-31T01:00:00+00:00",),
        )
    finally:
        await store.close()
    await _storage_collect(path)
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "retain"
    assert report["results"][0]["status"] == "retained_by_policy"
    assert report["rows_removed"] == 0
    with pytest.raises(SystemExit):
        await _storage_collect(path, mode="relay-only", apply=True)
    capsys.readouterr()
    await _storage_collect(path, mode="relay-only")
    preview = json.loads(capsys.readouterr().out)
    assert preview["results"][0]["status"] == "dry_run"
    assert preview["logical_bytes_removed"] == 0
    reopened = await SQLiteStorage.open_readonly(path)
    try:
        assert (
            await reopened._read_one(
                "SELECT body FROM event_body_content WHERE event_id='secret'"
            )
        )["body"] == "top-secret"
    finally:
        await reopened.close()
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    applied = json.loads(capsys.readouterr().out)
    assert applied["results"][0]["status"] == "collected"
    assert applied["logical_bytes_removed"] == len("top-secret")
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    again = json.loads(capsys.readouterr().out)
    assert again["results"][0]["status"] == "already_collected"


async def test_collection_cli_rejects_invalid_batch_limits_and_missing_file(tmp_path):
    path = str(tmp_path / "missing.db")
    with pytest.raises(SystemExit):
        await _storage_collect(path, limit=0)
    with pytest.raises(SystemExit):
        await _storage_collect(path, limit=101)
    with pytest.raises(SystemExit):
        await _storage_collect(path, mode="relay-only", apply=True, yes=True)


def _validate_batch(report):
    from pathlib import Path

    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    schemas = Path(__file__).resolve().parents[1] / "docs/schemas"
    batch = json.loads((schemas / "content-collection-batch.schema.json").read_text())
    event = json.loads((schemas / "event-content-collection.schema.json").read_text())
    Draft202012Validator.check_schema(batch)
    Draft202012Validator.check_schema(event)
    registry = Registry().with_resource(event["$id"], Resource.from_contents(event))
    Draft202012Validator(batch, registry=registry).validate(report)


async def test_collection_batch_schema_and_advisory_preview(tmp_path, capsys):
    path = str(tmp_path / "report.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="candidate"),
            payload={"body": "private"},
            metadata={},
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id='candidate'"
        )
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only")
    preview = json.loads(capsys.readouterr().out)
    _validate_batch(preview)
    assert preview["results"][0]["candidate_rows"] == 1
    assert preview["results"][0]["candidate_logical_bytes"] == len("private")
    assert preview["results"][0]["blockers"] == []
    assert preview["unmeasured"] and preview["events_refused"] == 0
    await _storage_collect(path, mode="retain")
    retained = json.loads(capsys.readouterr().out)
    _validate_batch(retained)
    assert retained["results"][0]["status"] == "retained_by_policy"
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    applied = json.loads(capsys.readouterr().out)
    _validate_batch(applied)
    assert applied["results"][0]["status"] == "collected"
    assert applied["logical_bytes_removed"] == len("private")
    await _storage_collect(path, mode="retain", after="candidate")
    empty = json.loads(capsys.readouterr().out)
    _validate_batch(empty)
    assert empty["results"] == [] and empty["unmeasured"]


async def test_collection_batch_reports_partial_commits_and_refusals(tmp_path, capsys):
    path = str(tmp_path / "partial.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        for event_id in ("first", "second"):
            event = msgspec.structs.replace(
                make_storage_event(event_id=event_id),
                payload={"body": "private"} if event_id == "first" else {},
                metadata={},
            )
            await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id IN ('first','second')"
        )
        await store._write(
            "INSERT INTO event_body_content(event_id,field,body,created_at) "
            "VALUES ('second','body','DO-NOT-DELETE','2026-10-31')"
        )
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only", apply=True, yes=True)
    report = json.loads(capsys.readouterr().out)
    _validate_batch(report)
    assert [r["status"] for r in report["results"]] == ["collected", "refused"]
    assert report["events_refused"] == 1
    assert report["next_after"] == "second"
    assert report["rows_removed"] == 1
    assert report["logical_bytes_removed"] == len("private")
    assert "DO-NOT-DELETE" not in json.dumps(report)
    reopened = await SQLiteStorage.open_readonly(path)
    try:
        stored = await reopened._read_one(
            "SELECT body FROM event_body_content WHERE event_id='second'"
        )
        assert stored["body"] == "DO-NOT-DELETE"
    finally:
        await reopened.close()


async def test_collection_cli_rejects_existing_empty_database_without_mutation(
    tmp_path, capsys
):
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(SystemExit):
        await _storage_collect(str(path), mode="relay-only", apply=True, yes=True)
    assert path.stat().st_size == 0
    assert not (tmp_path / "empty.db-wal").exists()
    assert "existing SQLite database required" in capsys.readouterr().err


async def test_collection_cli_accepts_long_existing_event_id_and_resume_cursor(
    tmp_path, capsys
):
    path = str(tmp_path / "long-id.db")
    event_id = "event-" + "q" * 580
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id=event_id), payload={"body": "text"}, metadata={}
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id=?",
            (event_id,),
        )
    finally:
        await store.close()
    await _storage_collect(path, mode="relay-only")
    report = json.loads(capsys.readouterr().out)
    _validate_batch(report)
    assert report["results"][0]["status"] == "dry_run"
    assert report["next_after"] == event_id
    await _storage_collect(path, mode="relay-only", after=event_id)
    resumed = json.loads(capsys.readouterr().out)
    _validate_batch(resumed)
    assert resumed["events_examined"] == 0
