"""Transactional prerelease content dependencies and separate text storage."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.events import EventRelation
from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _item(event_id: str, target: str) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=f"outbox-{event_id}-{target}",
        event_id=event_id,
        delivery_plan_id=f"plan-{target}",
        route_id="route1",
        target_adapter="fake_presentation",
        target_channel=target,
    )


async def test_body_and_text_bytes_are_separate_and_preserved(tmp_path):
    store = SQLiteStorage(str(tmp_path / "authority.db"))
    await store.initialize()
    event = msgspec.structs.replace(
        make_storage_event(event_id="split"),
        payload={
            "body": "confidential body",
            "text": "legacy duplicate",
            "kind": "user",
        },
    )
    try:
        await store.admit_ingress(event, None, "live")
        row = await store._read_one(
            "SELECT payload FROM canonical_events WHERE event_id='split'"
        )
        assert "confidential" not in row["payload"]
        assert "legacy duplicate" not in row["payload"]
        parts = await store._read_all(
            "SELECT field, body FROM event_body_content WHERE event_id='split' ORDER BY field"
        )
        assert [(r["field"], r["body"]) for r in parts] == [
            ("body", "confidential body"),
            ("text", "legacy duplicate"),
        ]
        assert (await store.get_metadata("split")).payload == {}
        restored = await store.read_event_for_ingress("split", now=_now())
        assert restored.status == "retained" and restored.event.payload == event.payload
        assert await store.get_body_presence_at_admission("split") is True
        assert (await store.list_mandatory_content_dependencies("split"))[0][
            0
        ] == "ingress"
    finally:
        await store.close()


async def test_empty_body_and_absent_body_are_distinct(tmp_path):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    try:
        for eid, body in [("empty", {"body": ""}), ("missing", {})]:
            await store.admit_ingress(
                msgspec.structs.replace(make_storage_event(event_id=eid), payload=body),
                None,
                "live",
            )
        assert await store.get_body_presence_at_admission("empty") is True
        assert await store.get_body_presence_at_admission("missing") is False
        assert len(await store.list_mandatory_content_dependencies("empty")) == 1
        assert len(await store.list_mandatory_content_dependencies("missing")) == 1
    finally:
        await store.close()


async def test_relation_excerpt_is_separate_and_has_ingress_owner(tmp_path):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    relation = EventRelation(
        relation_type="reply",
        target_event_id="parent",
        target_native_ref=None,
        key=None,
        fallback_text="secret quote",
    )
    event = msgspec.structs.replace(
        make_storage_event(event_id="reply"), payload={}, relations=(relation,)
    )
    try:
        await store.admit_ingress(event, None, "live")
        raw = await store._read_one(
            "SELECT * FROM event_relations WHERE event_id='reply'"
        )
        assert "fallback_text" not in raw
        assert (await store.get_metadata("reply")).relations[0].fallback_text is None
        assert (
            await store.read_event_for_ingress("reply", now=_now())
        ).event.relations[0].fallback_text == "secret quote"
        assert len(await store.list_mandatory_content_dependencies("reply")) == 1
        await store.store_relation("reply", relation)
        assert len(await store.list_relations("reply")) == 2
    finally:
        await store.close()


async def test_outbox_dependencies_are_atomic_independent_and_survive_restart(tmp_path):
    path = str(tmp_path / "state.db")
    store = SQLiteStorage(path)
    await store.initialize()
    event = msgspec.structs.replace(
        make_storage_event(event_id="shared"), payload={"body": "hello"}
    )
    try:
        await store.admit_ingress(event, None, "live")
        a, b = _item("shared", "a"), _item("shared", "b")
        await store.create_outbox_item(a)
        await store.create_outbox_item(b)
        assert len(await store.list_mandatory_content_dependencies("shared")) == 3
        assert (
            await store.read_body_for_outbox("shared", a.outbox_id, now=_now())
        ).body == "hello"
        assert (
            await store.read_body_for_outbox("shared", b.outbox_id, now=_now())
        ).body == "hello"
        assert (
            await store.read_body_for_outbox("shared", "other", now=_now())
        ).status == "denied"
        await store._write(
            "UPDATE delivery_outbox SET status='sent' WHERE outbox_id=?",
            (a.outbox_id,),
        )
        assert (
            await store.read_body_for_outbox("shared", a.outbox_id, now=_now())
        ).status == "denied"
        assert (
            await store.read_body_for_outbox("shared", b.outbox_id, now=_now())
        ).body == "hello"
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (
            await reopened.read_body_for_outbox("shared", b.outbox_id, now=_now())
        ).body == "hello"
        assert len(await reopened.list_mandatory_content_dependencies("shared")) == 3
    finally:
        await reopened.close()


async def test_content_deadline_denies_reads_and_new_dependency(tmp_path, monkeypatch):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    deadline = _now() + timedelta(minutes=5)
    event = msgspec.structs.replace(
        make_storage_event(event_id="ttl"), payload={"body": "sensitive"}
    )
    try:
        await store.admit_ingress(event, None, "live", content_deadline=deadline)
        item = _item("ttl", "first")
        await store.create_outbox_item(item)
        assert (
            await store.read_body_for_outbox(
                "ttl", item.outbox_id, now=deadline - timedelta(seconds=1)
            )
        ).body == "sensitive"
        assert (
            await store.read_body_for_outbox("ttl", item.outbox_id, now=deadline)
        ).status == "denied"
        # Expiry observation is durable: moving the wall clock back cannot restore access.
        assert (
            await store.read_body_for_outbox(
                "ttl", item.outbox_id, now=deadline - timedelta(seconds=1)
            )
        ).status == "denied"
        # A created_at supplied by a caller must not authorize a late outbox.
        monkeypatch.setattr(
            "medre.core.storage.sqlite._outbox._now_iso",
            lambda: (deadline + timedelta(seconds=1)).isoformat(
                timespec="microseconds"
            ),
        )
        late = _item("ttl", "late")
        await store.create_outbox_item(late)
        assert (
            await store.read_body_for_outbox(
                "ttl", late.outbox_id, now=deadline - timedelta(seconds=1)
            )
        ).status == "denied"
        assert len(await store.list_mandatory_content_dependencies("ttl")) == 2
        # An expired attempt to stage an outbox permanently seals the source,
        # even if the process clock later moves backwards.
        monkeypatch.setattr(
            "medre.core.storage.sqlite._outbox._now_iso",
            lambda: (deadline - timedelta(seconds=5)).isoformat(
                timespec="microseconds"
            ),
        )
        rollback = _item("ttl", "clock-backwards")
        await store.create_outbox_item(rollback)
        assert (
            await store.read_body_for_outbox(
                "ttl", rollback.outbox_id, now=deadline - timedelta(seconds=5)
            )
        ).status == "denied"
    finally:
        await store.close()


async def test_duplicate_id_does_not_replace_content_or_duplicate_hold(tmp_path):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="dup"), payload={"body": "original"}
            ),
            None,
            "live",
        )
        duplicate = await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="dup"), payload={"body": "replacement"}
            ),
            None,
            "live",
        )
        assert duplicate.created is False
        assert (await store.get_metadata("dup")).payload == {}
        assert (await store.read_event_for_ingress("dup", now=_now())).event.payload[
            "body"
        ] == "original"
        assert len(await store.list_mandatory_content_dependencies("dup")) == 1
    finally:
        await store.close()


async def test_post_admission_excerpt_gets_atomic_append_owner(tmp_path):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="late-quote"), payload={}
            ),
            None,
            "live",
        )
        before = await store.list_mandatory_content_dependencies("late-quote")
        assert len(before) == 1
        relation = EventRelation(
            relation_type="reply",
            target_event_id="target",
            target_native_ref=None,
            key=None,
            fallback_text="a late secret",
        )
        await store.store_relation("late-quote", relation)
        owners = await store.list_mandatory_content_dependencies("late-quote")
        assert owners == before
        assert (await store.get_metadata("late-quote")).relations[
            0
        ].fallback_text is None
        raw_excerpts = await store._read_all(
            "SELECT excerpt FROM event_relation_excerpts"
        )
        assert [row["excerpt"] for row in raw_excerpts] == ["a late secret"]
    finally:
        await store.close()


async def test_replay_generation_reuses_admission_hold_only(tmp_path):
    store = SQLiteStorage(str(tmp_path / "replay.db"))
    await store.initialize()
    try:
        deadline = _now() + timedelta(minutes=30)
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="replay-source"),
                payload={"body": "live only"},
            ),
            None,
            "live",
            content_deadline=deadline,
        )
        from dataclasses import replace

        replay = replace(
            _item("replay-source", "target"),
            dispatch_source="replay",
            replay_run_id="run-1",
        )
        stored = await store.create_outbox_item(replay, allocate_new_generation=True)
        assert stored.dispatch_source == "replay"
        # A replay generation never mints its own target-scoped hold.
        assert not any(
            kind == "outbox" and owner == replay.outbox_id
            for kind, owner, _, _ in await store.list_mandatory_content_dependencies(
                "replay-source"
            )
        )
        # While the admission hold is live, replay dispatch is authorized by
        # that hold alone: no outbox dependency exists to inherit.
        assert (
            await store.read_event_for_outbox(
                "replay-source", replay.outbox_id, now=_now()
            )
        ).status == "retained"
        assert (
            await store.read_body_for_outbox(
                "replay-source", replay.outbox_id, now=_now()
            )
        ).status == "retained"
        # Once the admission hold expires, replay fails closed: observation
        # releases the source durably and replay can no longer dispatch bytes.
        late = deadline + timedelta(minutes=1)
        assert (
            await store.read_event_for_outbox(
                "replay-source", replay.outbox_id, now=late
            )
        ).status == "denied"
        assert (
            await store.read_body_for_outbox(
                "replay-source", replay.outbox_id, now=_now()
            )
        ).status == "denied"
    finally:
        await store.close()


async def test_secondary_text_has_owner_even_when_body_identity_is_absent(tmp_path):
    store = SQLiteStorage(str(tmp_path / "secondary.db"))
    await store.initialize()
    deadline = _now() + timedelta(minutes=5)
    try:
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="secondary"),
                payload={"body": None, "text": "retained secondary text"},
            ),
            None,
            "live",
            content_deadline=deadline,
        )
        assert await store.get_body_presence_at_admission("secondary") is False
        assert len(await store.list_mandatory_content_dependencies("secondary")) == 1
        item = _item("secondary", "target")
        await store.create_outbox_item(item)
        assert (
            await store.read_event_for_ingress("secondary", now=_now())
        ).event.payload["text"] == "retained secondary text"
        assert (
            await store.read_event_for_ingress("secondary", now=deadline)
        ).status == "denied"
        assert (
            await store.read_event_for_outbox("secondary", item.outbox_id, now=_now())
        ).status == "denied"
        await store._write(
            "UPDATE delivery_outbox SET status='in_progress' WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert not await store.authorize_native_submission(
            "secondary",
            item.outbox_id,
            worker_id="unowned",
            attempt_number=1,
            now=_now(),
        )
    finally:
        await store.close()


@pytest.mark.parametrize("content", ["body", "excerpt"])
async def test_missing_source_dependency_never_authorizes_content(tmp_path, content):
    store = SQLiteStorage(str(tmp_path / "missing-owner.db"))
    await store.initialize()
    try:
        relation = EventRelation(
            relation_type="reply",
            target_event_id="parent",
            target_native_ref=None,
            key=None,
            fallback_text="private excerpt",
        )
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="missing-owner"),
                payload={"body": "private body"} if content == "body" else {},
                relations=() if content == "body" else (relation,),
            ),
            None,
            "live",
        )
        item = _item("missing-owner", "target")
        await store.create_outbox_item(item)
        # Simulate an inconsistent owner inventory without deleting retained bytes.
        await store._write(
            "DELETE FROM event_content_dependencies WHERE event_id=?", (item.event_id,)
        )
        assert (
            await store.read_event_for_ingress(item.event_id, now=_now())
        ).status == "denied"
        assert (
            await store.read_event_for_outbox(item.event_id, item.outbox_id, now=_now())
        ).status == "denied"
        await store._write(
            "UPDATE delivery_outbox SET status='in_progress' WHERE outbox_id=?",
            (item.outbox_id,),
        )
        assert not await store.authorize_native_submission(
            item.event_id,
            item.outbox_id,
            worker_id="unowned",
            attempt_number=1,
            now=_now(),
        )
    finally:
        await store.close()


async def test_refused_optional_hold_persists_observed_source_expiry(tmp_path):
    from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold

    store = SQLiteStorage(str(tmp_path / "optional.db"))
    await store.initialize()
    deadline = _now() + timedelta(minutes=5)
    try:
        await store.admit_ingress(
            make_storage_event(event_id="optional"),
            None,
            "live",
            content_deadline=deadline,
        )
        now = deadline + timedelta(seconds=1)
        policy = AvailabilityPolicy(
            hard_ceiling=now + timedelta(hours=1),
            allowed_optional_consumers=frozenset({"reader"}),
            max_optional_hold=timedelta(minutes=10),
        )
        hold = ContentHold(
            consumer="reader",
            scope="body",
            mandatory=False,
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )
        with pytest.raises(ValueError, match="Expired mandatory"):
            await store.issue_optional_body_hold("optional", hold, policy, now=now)
        assert (await store.list_mandatory_content_dependencies("optional"))[0][
            3
        ] is True
        assert (
            await store.read_event_for_ingress(
                "optional", now=deadline - timedelta(seconds=1)
            )
        ).status == "denied"
    finally:
        await store.close()


@pytest.mark.parametrize(
    "corrupt_sql",
    [
        "DELETE FROM event_body_content WHERE event_id='corrupt-body'",
        "UPDATE event_body_content SET body=CAST(body AS BLOB) WHERE event_id='corrupt-body'",
    ],
)
async def test_owned_event_read_refuses_missing_or_nontext_body(tmp_path, corrupt_sql):
    from medre.core.storage.backend import StorageError

    store = SQLiteStorage(str(tmp_path / "corrupt.db"))
    await store.initialize()
    try:
        await store.admit_ingress(
            make_storage_event(event_id="corrupt-body"), None, "live"
        )
        item = _item("corrupt-body", "target")
        await store.create_outbox_item(item)
        await store._write(corrupt_sql)
        with pytest.raises(StorageError, match="body|content"):
            await store.read_event_for_ingress(item.event_id, now=_now())
        with pytest.raises(StorageError, match="body|content"):
            await store.read_event_for_outbox(item.event_id, item.outbox_id, now=_now())
    finally:
        await store.close()


@pytest.mark.parametrize("operation", ["admit", "outbox"])
async def test_dependency_insert_failure_rolls_back_its_owner(tmp_path, operation):
    from medre.core.storage.backend import StorageError

    store = SQLiteStorage(str(tmp_path / "atomic.db"))
    await store.initialize()
    event = make_storage_event(event_id="atomic")
    try:
        if operation == "outbox":
            await store.admit_ingress(event, None, "live")
        await store._write(
            "CREATE TRIGGER refuse_dependency BEFORE INSERT ON event_content_dependencies "
            "BEGIN SELECT RAISE(ABORT, 'injected dependency failure'); END"
        )
        with pytest.raises(
            (StorageError, sqlite3.IntegrityError), match="injected dependency failure"
        ):
            if operation == "admit":
                await store.admit_ingress(event, None, "live")
            else:
                await store.create_outbox_item(_item(event.event_id, "target"))
        if operation == "admit":
            for sql in (
                "SELECT * FROM canonical_events",
                "SELECT * FROM event_body_identity",
                "SELECT * FROM event_body_content",
                "SELECT * FROM durable_ingress_work",
                "SELECT * FROM event_content_dependencies",
            ):
                assert not await store._read_all(sql)
        else:
            assert not await store.list_outbox_items_for_event(event.event_id)
            assert (
                len(await store.list_mandatory_content_dependencies(event.event_id))
                == 1
            )
    finally:
        await store.close()


async def test_outbox_at_exact_deadline_does_not_adopt_owner(tmp_path, monkeypatch):
    store = SQLiteStorage(str(tmp_path / "exact-deadline.db"))
    await store.initialize()
    deadline = (_now() + timedelta(minutes=5)).replace(microsecond=0)
    try:
        await store.admit_ingress(
            make_storage_event(event_id="exact"),
            None,
            "live",
            content_deadline=deadline,
        )
        # The general timestamp helper omits a fractional part at whole seconds;
        # lexical comparisons must still respect the exact UTC instant.
        monkeypatch.setattr(
            "medre.core.storage.sqlite._outbox._now_iso", lambda: deadline.isoformat()
        )
        await store.create_outbox_item(_item("exact", "target"))
        owners = await store.list_mandatory_content_dependencies("exact")
        assert len(owners) == 1
        assert owners[0][3] is True
    finally:
        await store.close()


async def test_owned_event_read_refuses_nontext_excerpt(tmp_path):
    from medre.core.storage.backend import StorageError

    store = SQLiteStorage(str(tmp_path / "corrupt-excerpt.db"))
    await store.initialize()
    try:
        relation = EventRelation(
            relation_type="reply",
            target_event_id="parent",
            target_native_ref=None,
            key=None,
            fallback_text="private excerpt",
        )
        await store.admit_ingress(
            msgspec.structs.replace(
                make_storage_event(event_id="corrupt-excerpt"),
                relations=(relation,),
            ),
            None,
            "live",
        )
        item = _item("corrupt-excerpt", "target")
        await store.create_outbox_item(item)
        await store._write(
            "UPDATE event_relation_excerpts SET excerpt=CAST(excerpt AS BLOB)"
        )
        with pytest.raises(StorageError, match="excerpt content"):
            await store.read_event_for_ingress(item.event_id, now=_now())
        with pytest.raises(StorageError, match="excerpt content"):
            await store.read_event_for_outbox(item.event_id, item.outbox_id, now=_now())
    finally:
        await store.close()
