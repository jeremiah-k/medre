"""Grant callers receive the storage error contract without losing SQL causes."""

import sqlite3
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.storage.backend import StorageError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def _granted_store(tmp_path):
    store = SQLiteStorage(str(tmp_path / "state.sqlite"))
    await store.initialize()
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    policy = AvailabilityPolicy(
        now + timedelta(hours=1), timedelta(minutes=10), frozenset({"history"})
    )
    hold = ContentHold("history", "body", now + timedelta(minutes=5), issued_at=now)
    await store.append(
        msgspec.structs.replace(
            make_storage_event(event_id="body"), payload={"body": "private"}
        )
    )
    await store.issue_optional_body_hold("body", hold, policy, now=now)
    return store, now, policy, hold


@pytest.mark.parametrize("operation", ["issue", "revoke", "read"])
async def test_sqlite_grant_failures_use_storage_error(tmp_path, operation):
    store, now, policy, hold = await _granted_store(tmp_path)

    def deny_content_access(action, table, column, database, source):
        if table in {"event_content_access_policies", "event_content_access_grants"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    try:
        await store._run_in_thread(
            store._require_db().set_authorizer, deny_content_access
        )
        with pytest.raises(
            StorageError, match="Content grant operation failed"
        ) as error:
            if operation == "issue":
                await store.issue_optional_body_hold("body", hold, policy, now=now)
            elif operation == "revoke":
                await store.revoke_optional_body_hold("body", "history", now=now)
            else:
                await store.read_body_for_consumer("body", "history", now=now)
        assert isinstance(error.value.__cause__, sqlite3.Error)
    finally:
        await store._run_in_thread(store._require_db().set_authorizer, None)
        await store.close()


@pytest.mark.parametrize(
    "corrupt_sql",
    [
        "DELETE FROM event_body_content WHERE event_id='body'",
        "UPDATE event_body_content SET body=CAST('kept' AS BLOB) "
        "WHERE event_id='body'",
    ],
)
async def test_granted_read_reports_corrupt_content_as_storage_error(
    tmp_path, corrupt_sql
):
    store, now, _, _ = await _granted_store(tmp_path)
    try:
        await store._write(corrupt_sql)
        with pytest.raises(StorageError):
            await store.read_body_for_consumer("body", "history", now=now)
    finally:
        await store.close()


async def test_granted_read_reports_missing_admission_fact_as_corruption(tmp_path):
    store, now, _, _ = await _granted_store(tmp_path)
    try:
        # Dependencies reference the manifest with enforced foreign keys;
        # losing the manifest alone is out-of-band corruption.
        db = sqlite3.connect(str(tmp_path / "state.sqlite"))
        try:
            db.execute("DELETE FROM event_body_identity WHERE event_id='body'")
            db.commit()
        finally:
            db.close()
        with pytest.raises(StorageError, match="no valid body identity"):
            await store.read_body_for_consumer("body", "history", now=now)
        # A requester without a grant learns nothing about the broken manifest.
        assert (
            await store.read_body_for_consumer("body", "other", now=now)
        ).status == "denied"
    finally:
        await store.close()
