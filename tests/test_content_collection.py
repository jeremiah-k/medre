"""Contract tests for strictly bounded logical collection; no physical erasure."""

import json
from pathlib import Path

import msgspec
import pytest
from jsonschema import Draft202012Validator

from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def _isolated(store, event_id="candidate", body="private"):
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": body}, metadata={}
    )
    await store.append(event)
    # Controlled settlement fixture: append owners have no runtime terminal stage.
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        ("2026-10-31T01:00:00+00:00", event_id),
    )


async def test_collection_dry_run_transactional_idempotent_and_restart(tmp_path):
    path = str(tmp_path / "collection.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await _isolated(store)
        preview = await store.collect_event_content("candidate")
        assert preview["status"] == "dry_run"
        assert preview["candidate_rows"] == 1
        assert preview["candidate_logical_bytes"] == len("private")
        assert preview["rows_removed"] == 0
        assert json.dumps(preview).find("private") == -1
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "docs/schemas/event-content-collection.schema.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(preview)
        assert (await store.collect_event_content("candidate", dry_run=False))[
            "status"
        ] == "collected"
        assert (await store.collect_event_content("candidate", dry_run=False))[
            "status"
        ] == "already_collected"
        row = await store._read_one(
            "SELECT COUNT(*) AS n FROM event_body_content WHERE event_id='candidate'"
        )
        assert row["n"] == 0
        assert await store.get_metadata("candidate") is not None
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (await reopened.collect_event_content("candidate"))[
            "status"
        ] == "already_collected"
    finally:
        await reopened.close()


async def test_collection_rejects_active_owner_opaque_and_incoming_quote(tmp_path):
    store = SQLiteStorage(str(tmp_path / "guard.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="active"))
        assert (
            "unsettled_mandatory_owners"
            in (await store.collect_event_content("active", dry_run=False))["blockers"]
        )
        await _isolated(store, "opaque")
        await store._write(
            "UPDATE canonical_events SET metadata=? WHERE event_id='opaque'",
            ('{"secret":"not-safe"}',),
        )
        report = await store.collect_event_content("opaque", dry_run=False)
        assert report["status"] == "blocked"
        assert "opaque_envelope" in report["blockers"]
        assert "not-safe" not in json.dumps(report)
        assert (
            await store._read_one(
                "SELECT body FROM event_body_content WHERE event_id='opaque'"
            )
        )["body"] == "private"
        with pytest.raises(StorageError, match="admitted event"):
            await store.collect_event_content("does-not-exist", dry_run=False)
    finally:
        await store.close()


async def test_collection_rolls_back_on_audit_write_failure(tmp_path):
    store = SQLiteStorage(str(tmp_path / "atomic.db"))
    await store.initialize()
    try:
        await _isolated(store)
        await store._write(
            "CREATE TRIGGER fail_collection BEFORE INSERT ON "
            "event_content_collection_log BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
        with pytest.raises(StorageError, match="collection failed"):
            await store.collect_event_content("candidate", dry_run=False)
        row = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='candidate'"
        )
        assert row["body"] == "private"
        await store._write("DROP TRIGGER fail_collection")
        assert (await store.collect_event_content("candidate", dry_run=False))[
            "status"
        ] == "collected"
    finally:
        await store.close()


async def test_collection_rechecks_after_preview_before_apply(tmp_path):
    store = SQLiteStorage(str(tmp_path / "stale-preview.db"))
    await store.initialize()
    try:
        await _isolated(store)
        assert (await store.collect_event_content("candidate"))["status"] == "dry_run"
        # Simulate an owner becoming live between preview and collection.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=NULL "
            "WHERE event_id='candidate' AND owner_kind='append'"
        )
        result = await store.collect_event_content("candidate", dry_run=False)
        assert result["status"] == "blocked"
        assert "unsettled_mandatory_owners" in result["blockers"]
        assert (
            await store._read_one(
                "SELECT body FROM event_body_content WHERE event_id='candidate'"
            )
        )["body"] == "private"
    finally:
        await store.close()


async def test_two_storage_handles_collect_exactly_once(tmp_path):
    import asyncio

    path = str(tmp_path / "concurrent.db")
    first = SQLiteStorage(path)
    await first.initialize()
    await _isolated(first)
    second = SQLiteStorage(path)
    await second.initialize()
    try:
        results = await asyncio.gather(
            first.collect_event_content("candidate", dry_run=False),
            second.collect_event_content("candidate", dry_run=False),
        )
        assert sorted(r["status"] for r in results) == [
            "already_collected",
            "collected",
        ]
        assert sum(r["rows_removed"] for r in results) == 1
        count = await first._read_one(
            "SELECT COUNT(*) AS n FROM event_content_collection_log"
        )
        assert count["n"] == 1
    finally:
        await first.close()
        await second.close()


async def test_structured_relation_survives_collected_excerpt(tmp_path):
    store = SQLiteStorage(str(tmp_path / "relation.db"))
    await store.initialize()
    try:
        await _isolated(store)
        await store._write(
            "INSERT INTO event_relations "
            "(event_id,relation_type,target_event_id,metadata,created_at) "
            "VALUES ('candidate','reply','missing-parent','{}','2026-10-31')"
        )
        await store._write(
            "INSERT INTO event_relation_excerpts (relation_id,excerpt) "
            "SELECT id,'SENSITIVE-QUOTE' FROM event_relations WHERE event_id='candidate'"
        )
        result = await store.collect_event_content("candidate", dry_run=False)
        assert result["status"] == "collected"
        assert result["rows_removed"] == 2
        assert (
            await store._read_one(
                "SELECT COUNT(*) AS n FROM event_relations WHERE event_id='candidate'"
            )
        )["n"] == 1
        assert (
            await store._read_one("SELECT COUNT(*) AS n FROM event_relation_excerpts")
        )["n"] == 0
    finally:
        await store.close()


async def test_opaque_relation_key_blocks_quote_collection(tmp_path):
    store = SQLiteStorage(str(tmp_path / "reaction.db"))
    await store.initialize()
    try:
        await _isolated(store)
        await store._write(
            "INSERT INTO event_relations "
            "(event_id,relation_type,key,created_at) "
            "VALUES ('candidate','reaction','SECRET-KEY','2026-10-31')"
        )
        result = await store.collect_event_content("candidate", dry_run=False)
        assert result["status"] == "blocked"
        assert "opaque_relation_records" in result["blockers"]
        assert "SECRET-KEY" not in json.dumps(result)
    finally:
        await store.close()


async def test_unknown_relation_type_is_not_treated_as_safe_identity(tmp_path):
    store = SQLiteStorage(str(tmp_path / "bad-relation.db"))
    await store.initialize()
    try:
        await _isolated(store)
        await store._write(
            "INSERT INTO event_relations "
            "(event_id,relation_type,created_at) "
            "VALUES ('candidate','UNTRUSTED-CONTENT','2026-10-31')"
        )
        result = await store.collect_event_content("candidate", dry_run=False)
        assert result["status"] == "blocked"
        assert "opaque_relation_records" in result["blockers"]
        assert "UNTRUSTED-CONTENT" not in json.dumps(result)
    finally:
        await store.close()


async def test_collection_rejects_unexplained_body_when_admission_says_absent(tmp_path):
    store = SQLiteStorage(str(tmp_path / "absent-with-copy.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="absent"), payload={}, metadata={}
        )
        await store.append(event)
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-31' "
            "WHERE event_id='absent'"
        )
        await store._write(
            "INSERT INTO event_body_content(event_id,field,body,created_at) "
            "VALUES ('absent','body','UNEXPLAINED','2026-10-31')"
        )
        with pytest.raises(StorageError, match="admission recorded no body"):
            await store.collect_event_content("absent", dry_run=False)
        with pytest.raises(StorageError, match="admission recorded no body"):
            await store.collect_event_content("absent", dry_run=True)
        body = await store._read_one(
            "SELECT body FROM event_body_content WHERE event_id='absent'"
        )
        assert body["body"] == "UNEXPLAINED"
        assert not await store._read_one(
            "SELECT 1 FROM event_content_collection_log WHERE event_id='absent'"
        )
    finally:
        await store.close()


def test_collection_ddl_has_one_authoritative_definition():
    from medre.core.storage.sqlite.schema import _COLLECTION_LOG_DDL, _SCHEMA

    assert _SCHEMA.count("CREATE TABLE IF NOT EXISTS event_content_collection_log") == 1
    assert _COLLECTION_LOG_DDL in _SCHEMA
    assert "CHECK (policy = 'relay-only')" in _COLLECTION_LOG_DDL
