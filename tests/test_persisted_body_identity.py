"""Canonical body identity is a single atomic admission fact, not a read grant."""

from __future__ import annotations

import sqlite3

import msgspec
import pytest

from medre.core.events import NativeMessageRef
from medre.core.storage.backend import DuplicateEventError, StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"body": "hello"}, True),
        ({"body": ""}, True),
        ({"text": "legacy renderer text"}, True),
        ({"text": ""}, True),
        ({"body": None, "text": "not an authorized substitute"}, False),
        ({"body": 42, "text": "not a substitute"}, False),
        ({"text": 42}, False),
        ({"kind": "reaction"}, False),
    ],
)
async def test_append_atomically_records_explicit_body_presence(
    tmp_path, payload, expected
):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="body-case"), payload=payload
        )
        assert await storage.get_body_presence_at_admission(event.event_id) is None
        await storage.append(event)
        assert await storage.get_body_presence_at_admission(event.event_id) == expected
        assert (await storage.get(event.event_id)) is not None
        row = await storage._read_one(
            "SELECT version, present_at_admission FROM event_body_identity WHERE event_id=?",
            (event.event_id,),
        )
        assert row == {"version": 1, "present_at_admission": int(expected)}
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("original_payload", "duplicate_payload", "expected"),
    [({"body": "original"}, {}, True), ({}, {"body": "later"}, False)],
)
async def test_durable_ingress_and_duplicate_admission_keep_original_fact(
    tmp_path, original_payload, duplicate_payload, expected
):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="ingress-1"), payload=original_payload
        )
        native = NativeMessageRef(
            id="native-ref-first",
            event_id=event.event_id,
            adapter="matrix",
            native_channel_id="!room:example.org",
            native_message_id="$native-original",
            native_thread_id=None,
            native_relation_id=None,
            direction="inbound",
        )
        first = await storage.admit_ingress(event, native, "live")
        assert first.created
        assert first.event_id == event.event_id
        assert await storage.get_body_presence_at_admission(event.event_id) == expected
        # A distinct proposed ID for the same persisted native identity must
        # resolve to the first event without creating or replacing body facts.
        altered = msgspec.structs.replace(
            event, event_id="ingress-2", payload=duplicate_payload
        )
        duplicate_native = msgspec.structs.replace(
            native, id="native-ref-second", event_id=altered.event_id
        )
        repeated = await storage.admit_ingress(altered, duplicate_native, "live")
        assert not repeated.created
        assert repeated.event_id == event.event_id
        assert await storage.get_body_presence_at_admission(event.event_id) == expected
        assert await storage.get_body_presence_at_admission(altered.event_id) is None
        count = await storage._read_one(
            "SELECT count(*) AS n FROM event_body_identity WHERE event_id=?",
            (event.event_id,),
        )
        assert count == {"n": 1}
        assert await storage._read_one(
            "SELECT count(*) AS n FROM event_body_identity", ()
        ) == {"n": 1}
    finally:
        await storage.close()


@pytest.mark.parametrize("event_id", [" " * 3, "x" * 513, "\t\n"])
async def test_content_lookup_accepts_every_admitted_event_id(tmp_path, event_id):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        event = make_storage_event(event_id=event_id)
        await storage.append(event)
        assert await storage.get_body_presence_at_admission(event_id) is True
    finally:
        await storage.close()


@pytest.mark.parametrize("bad_id", ["", None, 42])
async def test_content_lookup_rejects_invalid_event_identifiers(tmp_path, bad_id):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        with pytest.raises(ValueError, match="non-empty event identifier"):
            await storage.get_body_presence_at_admission(bad_id)
    finally:
        await storage.close()


async def test_admission_failure_leaves_no_orphan_content_identity(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        event = make_storage_event(event_id="collision")
        await storage.append(event)
        with pytest.raises(DuplicateEventError):
            await storage.append(event)
        row = await storage._read_one(
            "SELECT count(*) AS n FROM event_body_identity WHERE event_id=?",
            (event.event_id,),
        )
        assert row == {"n": 1}
    finally:
        await storage.close()


async def test_persisted_identity_survives_reopen_without_exposing_body(tmp_path):
    path = str(tmp_path / "state.sqlite")
    storage = SQLiteStorage(path)
    await storage.initialize()
    await storage.append(
        msgspec.structs.replace(make_storage_event(event_id="no-body"), payload={})
    )
    await storage.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert await reopened.get_body_presence_at_admission("no-body") is False
        assert await reopened.get_body_presence_at_admission("not-an-event") is None
    finally:
        await reopened.close()


def test_missing_content_identity_table_refuses_stamped_old_shape(tmp_path):
    """Pre-release state is rejected, never automatically modified or migrated."""
    from medre.core.storage.sqlite.connection import sync_find_schema_shape_mismatch
    from medre.core.storage.sqlite.schema import _REQUIRED_COLUMNS

    db_path = str(tmp_path / "old.sqlite")
    db = sqlite3.connect(db_path)
    try:
        db.execute("CREATE TABLE _medre_schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO _medre_schema_meta VALUES ('schema_version', '1')")
        db.commit()
    finally:
        db.close()
    mismatch = sync_find_schema_shape_mismatch(db_path, _REQUIRED_COLUMNS)
    assert mismatch is not None
    assert "event_body_identity" in _REQUIRED_COLUMNS


async def test_missing_manifest_is_corruption_not_absent_content(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        event = make_storage_event(event_id="manifest-lost")
        await storage.append(event)
        # Content dependencies reference the manifest with enforced foreign
        # keys, so losing the manifest alone is out-of-band corruption.
        db = sqlite3.connect(str(tmp_path / "state.sqlite"))
        try:
            db.execute(
                "DELETE FROM event_body_identity WHERE event_id=?", (event.event_id,)
            )
            db.commit()
        finally:
            db.close()
        with pytest.raises(StorageError, match="no valid body identity"):
            await storage.get_body_presence_at_admission(event.event_id)
        assert await storage.get_body_presence_at_admission("truly-absent") is None
    finally:
        await storage.close()


async def test_open_refuses_existing_pre_release_db_without_identity_table(tmp_path):
    """Validation precedes DDL, so opening never installs missing tables."""
    from medre.core.storage.backend import PreReleaseSchemaMismatchError

    path = str(tmp_path / "old.sqlite")
    original = SQLiteStorage(path)
    await original.initialize()
    await original.close()
    db = sqlite3.connect(path)
    try:
        db.execute("DROP TABLE event_body_identity")
        db.commit()
    finally:
        db.close()

    restored = SQLiteStorage(path)
    with pytest.raises(PreReleaseSchemaMismatchError):
        await restored.initialize()
    db = sqlite3.connect(path)
    try:
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='event_body_identity'"
            ).fetchone()
            is None
        )
    finally:
        db.close()


@pytest.mark.parametrize("admission", ["append", "ingress"])
async def test_failed_body_insert_rolls_back_entire_admission(tmp_path, admission):
    """An error after identity insertion cannot leave a canonical event behind."""
    storage = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await storage.initialize()
    try:
        await storage._write(
            "CREATE TRIGGER reject_body AFTER INSERT ON event_body_identity "
            "BEGIN SELECT RAISE(ABORT, 'body admission refused'); END",
            (),
        )
        event = make_storage_event(event_id="rolled-back")
        with pytest.raises(StorageError, match="body admission refused"):
            if admission == "append":
                await storage.append(event)
            else:
                await storage.admit_ingress(event, None, "live")
        assert await storage.get(event.event_id) is None
        assert await storage.get_body_presence_at_admission(event.event_id) is None
        assert await storage._read_one(
            "SELECT count(*) AS n FROM event_body_identity", ()
        ) == {"n": 0}
        assert await storage._read_one(
            "SELECT count(*) AS n FROM durable_ingress_work", ()
        ) == {"n": 0}
    finally:
        await storage.close()
