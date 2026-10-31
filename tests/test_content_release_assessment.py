"""Read-only content collection eligibility is never a deletion capability."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import msgspec
import pytest
from jsonschema import Draft202012Validator

from medre.core.storage.backend import DeliveryOutboxItem, StorageError
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
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
        _validate_assessment(before)
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


def _validate_assessment(report):
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "docs/schemas/event-content-release-assessment.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(report)


async def test_release_assessment_eligible_subset_is_still_not_deletion_authority(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "eligible.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="metadata-only"), payload={}, metadata={}
        )
        await store.append(event)
        # Explicitly settle the append dependency in this controlled shape test.
        # There is no automatic append-owner lifecycle in prerelease MEDRE.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id=? AND owner_kind='append'",
            ("2026-10-31T00:00:00+00:00", "metadata-only"),
        )
        report = await store.assess_event_content_release("metadata-only")
        _validate_assessment(report)
        assert report["status"] == "unmeasured"
        assert report["core_subset_status"] == "eligible"
        assert report["classified_core_candidate"] is True
        assert report["blockers"] == []
        assert report["deletion_authorized"] is False
        assert report["retention_authorized"] is False
        assert {copy["classification"] for copy in report["copies"]} == {
            "bounded_candidate",
            "shared_reference",
            "authority_or_state",
            "requires_independent_policy",
            "identity_retained",
        }
    finally:
        await store.close()


async def test_release_assessment_lists_each_active_owner_and_grant(tmp_path):
    store = SQLiteStorage(str(tmp_path / "blocked.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="in-flight"), payload={}, metadata={}
        )
        await store.admit_ingress(event, None, "live")
        item = DeliveryOutboxItem(
            outbox_id="in-flight-outbox",
            event_id="in-flight",
            route_id="r",
            delivery_plan_id="plan",
            target_adapter="fake_presentation",
            target_channel="c",
        )
        await store.create_outbox_item(item)
        now = datetime.now(timezone.utc)
        policy = AvailabilityPolicy(
            hard_ceiling=now + timedelta(hours=2),
            max_optional_hold=timedelta(hours=1),
            allowed_optional_consumers=frozenset({"history"}),
        )
        await store.issue_optional_body_hold(
            "in-flight",
            ContentHold("history", "body", now + timedelta(minutes=10), issued_at=now),
            policy,
            now=now,
        )
        report = await store.assess_event_content_release("in-flight")
        _validate_assessment(report)
        assert {
            "active_ingress",
            "active_outbox",
            "unsettled_mandatory_owners",
            "unrevoked_optional_grants",
        } <= set(report["blockers"])
        assert report["core_subset_status"] == "blocked"
        assert report["deletion_authorized"] is False
    finally:
        await store.close()


async def test_incoming_quote_copy_blocks_retention_of_referenced_event(tmp_path):
    store = SQLiteStorage(str(tmp_path / "incoming.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="referenced"))
        await store.append(make_storage_event(event_id="quoting"))
        await store._write(
            "INSERT INTO event_relations (event_id, relation_type, target_event_id, "
            "created_at) VALUES (?, 'reply', ?, ?)",
            ("quoting", "referenced", "2026-10-31T00:00:00+00:00"),
        )
        await store._write(
            "INSERT INTO event_relation_excerpts (relation_id, excerpt) "
            "SELECT id, ? FROM event_relations WHERE event_id=?",
            ("SECRET-QUOTE", "quoting"),
        )
        report = await store.assess_event_content_release("referenced")
        _validate_assessment(report)
        assert "incoming_relation_excerpts" in report["blockers"]
        assert "SECRET-QUOTE" not in json.dumps(report)
    finally:
        await store.close()
