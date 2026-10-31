"""Read-only content collection eligibility is never a deletion capability."""

import json

import msgspec
import pytest

from medre.core.storage.backend import StorageError
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


async def test_event_release_assessment_is_metadata_only_and_fail_closed(tmp_path):
    store = SQLiteStorage(str(tmp_path / "authority.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="secret-event"),
            payload={"body": "VERY-PRIVATE-BODY", "custom": "SECRET-CUSTOM-METADATA"},
        )
        await store.admit_ingress(event, None, "live")
        before = await store.assess_event_content_release("secret-event")
        assert before["status"] == "blocked"
        assert "unsettled_mandatory_owners" in before["blockers"]
        assert "opaque_envelope" in before["blockers"]
        assert before["deletion_authorized"] is False
        assert before["retention_authorized"] is False
        encoded = json.dumps(before)
        assert "VERY-PRIVATE-BODY" not in encoded
        assert "SECRET-CUSTOM-METADATA" not in encoded
        assert "provider_native_queues_histories_and_peers" in before["unmeasured"]
        assert "sqlite_free_pages_and_wal" in before["unmeasured"]
        await store._write(
            "UPDATE event_content_dependencies SET released_at='2026-10-30T00:00:00+00:00' "
            "WHERE event_id='secret-event'"
        )
        after = await store.assess_event_content_release("secret-event")
        assert after["deletion_authorized"] is False
        assert "opaque_envelope" in after["blockers"]
        assert after["status"] == "blocked"
        before_copies = {r["surface"]: r["rows"] for r in before["copies"]}
        after_copies = {r["surface"]: r["rows"] for r in after["copies"]}
        assert before_copies["canonical_text"] == after_copies["canonical_text"] == 1
        assert after_copies["unsettled_mandatory_owners"] == 0
        with pytest.raises(StorageError, match="admitted event"):
            await store.assess_event_content_release("missing")
    finally:
        await store.close()


async def test_release_assessment_requires_no_writer_and_survives_restart(tmp_path):
    path = str(tmp_path / "authority.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="sample"))
        first = await store.assess_event_content_release("sample")
        readonly = await SQLiteStorage.open_readonly(path)
        try:
            assert await readonly.assess_event_content_release("sample") == first
        finally:
            await readonly.close()
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert await reopened.assess_event_content_release("sample") == first
    finally:
        await reopened.close()


async def test_shared_attachment_is_reported_but_never_projected(tmp_path):
    import hashlib

    store = SQLiteStorage(str(tmp_path / "shared.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="first"))
        await store.append(make_storage_event(event_id="second"))
        data = b"DO-NOT-LEAK-ATTACHMENT"
        ref = "sha256:" + hashlib.sha256(data).hexdigest()
        await store._write_batch(
            [
                (
                    "INSERT INTO attachment_blobs "
                    "(content_ref,size_bytes,created_at,data) VALUES (?,?,?,?)",
                    (ref, len(data), "2026-10-30T00:00:00+00:00", data),
                ),
                *[
                    (
                        "INSERT INTO event_attachment_associations "
                        "(event_id,content_ref,created_at) VALUES (?,?,?)",
                        (eid, ref, "2026-10-30T00:00:00+00:00"),
                    )
                    for eid in ("first", "second")
                ],
            ]
        )
        assessment = await store.assess_event_content_release("first")
        copies = {r["surface"]: r["rows"] for r in assessment["copies"]}
        assert copies["attachment_associations"] == 1
        assert copies["shared_attachment_references"] == 1
        assert assessment["status"] == "blocked"
        assert assessment["deletion_authorized"] is False
        assert data.decode() not in json.dumps(assessment)
        assert ref not in json.dumps(assessment)
    finally:
        await store.close()
