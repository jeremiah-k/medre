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
