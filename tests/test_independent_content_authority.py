"""Independent owners and terminal settlement remain durable and fail closed."""

from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.storage.backend import DeliveryOutboxItem
from medre.core.storage.content_availability import AvailabilityPolicy, ContentHold
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


def _event(event_id="owned"):
    return msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload={"body": "private"}
    )


def _outbox(event_id, target):
    return DeliveryOutboxItem(
        outbox_id=f"{event_id}-{target}",
        event_id=event_id,
        delivery_plan_id=f"plan-{target}",
        route_id="route",
        target_adapter="fake_presentation",
        target_channel=target,
    )


async def test_outbox_is_independent_but_replay_is_only_a_source_borrow(tmp_path):
    store = SQLiteStorage(str(tmp_path / "authority.db"))
    await store.initialize()
    try:
        await store.admit_ingress(_event(), None, "live")
        a, b = _outbox("owned", "a"), _outbox("owned", "b")
        await store.create_outbox_item(a)
        await store.create_outbox_item(b)
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id=? AND owner_kind='ingress'",
            (datetime.now(timezone.utc).isoformat(), "owned"),
        )
        assert (await store.read_event_for_ingress("owned")).status == "denied"
        assert (await store.read_event_for_replay("owned")).status == "denied"
        for item in (a, b):
            assert (
                await store.read_event_for_outbox("owned", item.outbox_id)
            ).event.payload["body"] == "private"
            assert (
                await store.read_body_for_outbox("owned", item.outbox_id)
            ).body == "private"
    finally:
        await store.close()


async def test_existing_optional_grant_survives_source_settlement_but_not_expiry(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "grant.db"))
    await store.initialize()
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    try:
        await store.append(_event())
        policy = AvailabilityPolicy(
            now + timedelta(hours=1), timedelta(minutes=20), frozenset({"history"})
        )
        hold = ContentHold(
            "history", "body", now + timedelta(minutes=10), issued_at=now
        )
        await store.issue_optional_body_hold("owned", hold, policy, now=now)
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? WHERE event_id=?",
            (now.isoformat(), "owned"),
        )
        assert (
            await store.read_body_for_consumer(
                "owned", "history", now=now + timedelta(minutes=5)
            )
        ).body == "private"
        assert (
            await store.read_body_for_consumer(
                "owned", "history", now=now + timedelta(minutes=10)
            )
        ).status == "denied"
    finally:
        await store.close()


async def test_reply_excerpt_uses_only_an_existing_bounded_quote_grant(tmp_path):
    path = str(tmp_path / "reply.db")
    store = SQLiteStorage(path)
    await store.initialize()
    now = datetime.now(timezone.utc)
    try:
        await store.admit_ingress(_event(), None, "live")
        policy = AvailabilityPolicy(
            now + timedelta(minutes=30),
            timedelta(minutes=10),
            frozenset({"reply_quotes"}),
        )
        hold = ContentHold(
            "reply_quotes", "body", now + timedelta(minutes=5), issued_at=now
        )
        await store.issue_optional_body_hold("owned", hold, policy, now=now)
        # Simulate a settled source directly: guarded ingress completion belongs
        # to the dependent terminal-settlement change, not this contract.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id=? AND owner_kind='ingress'",
            (now.isoformat(), "owned"),
        )
        assert (await store.read_event_for_replay("owned")).status == "denied"
        quote = await store.read_reply_context("owned", now=now + timedelta(minutes=1))
        assert quote.status == "retained"
        assert quote.event is not None and quote.event.payload["body"] == "private"
        assert (
            await store.read_reply_context("owned", now=now + timedelta(minutes=5))
        ).status == "denied"
        assert (
            await store.read_reply_context("owned", now=now + timedelta(minutes=1))
        ).status == "denied"
    finally:
        await store.close()


@pytest.mark.parametrize("refusal", ["replacement", "policy_change"])
async def test_rejected_grant_issue_cannot_undo_observed_optional_expiry(
    tmp_path, refusal
):
    path = str(tmp_path / "rejected-issue.db")
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        await store.append(_event())
        now = datetime.now(timezone.utc)
        policy = AvailabilityPolicy(
            now + timedelta(hours=1),
            timedelta(minutes=30),
            frozenset({"history", "export"}),
        )
        await store.issue_optional_body_hold(
            "owned",
            ContentHold("history", "body", now + timedelta(minutes=5), issued_at=now),
            policy,
            now=now,
        )
        at = now + timedelta(minutes=6)
        replacement = ContentHold(
            "history" if refusal == "replacement" else "export",
            "body",
            now + timedelta(minutes=12),
            issued_at=at,
        )
        if refusal == "policy_change":
            policy = AvailabilityPolicy(
                policy.hard_ceiling,
                timedelta(minutes=20),
                policy.allowed_optional_consumers,
            )
        with pytest.raises(ValueError):
            await store.issue_optional_body_hold("owned", replacement, policy, now=at)
        row = await store._read_one(
            "SELECT expired_at, revoked_at FROM event_content_access_grants "
            "WHERE event_id=? AND consumer=?",
            ("owned", "history"),
        )
        assert row["expired_at"] is not None
        assert row["revoked_at"] is None
        assert (
            await store.read_body_for_consumer(
                "owned", "history", now=now + timedelta(minutes=1)
            )
        ).status == "denied"
    finally:
        await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    try:
        assert (
            await reopened.read_body_for_consumer(
                "owned", "history", now=now + timedelta(minutes=1)
            )
        ).status == "denied"
    finally:
        await reopened.close()


async def test_body_quote_grant_does_not_authorize_opaque_sender_metadata(tmp_path):
    import sqlite3

    from medre.core.events.metadata import EventMetadata, NativeMetadata

    store = SQLiteStorage(str(tmp_path / "quote-scope.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            _event(),
            metadata=EventMetadata(
                native=NativeMetadata(data={"private_copy": "UNRELATED-NATIVE-BYTES"}),
                custom={"private_copy": "UNRELATED-CUSTOM-BYTES"},
            ),
        )
        await store.admit_ingress(event, None, "live")
        now = datetime.now(timezone.utc)
        await store.issue_optional_body_hold(
            "owned",
            ContentHold(
                "reply_quotes", "body", now + timedelta(minutes=5), issued_at=now
            ),
            AvailabilityPolicy(
                now + timedelta(hours=1),
                timedelta(minutes=10),
                frozenset({"reply_quotes"}),
            ),
            now=now,
        )
        source_quote = await store.read_reply_context("owned", now=now)
        assert (
            source_quote.event.metadata.native.data["private_copy"]
            == "UNRELATED-NATIVE-BYTES"
        )
        # Source settlement is exercised by the dependent lifecycle slice.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id=? AND owner_kind='ingress'",
            (now.isoformat(), "owned"),
        )

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and (table, column) == (
                "canonical_events",
                "metadata",
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        await store._run_in_thread(store._require_db().set_authorizer, authorize)
        try:
            quote = await store.read_reply_context(
                "owned", now=now + timedelta(seconds=1)
            )
            assert quote.status == "retained" and quote.event is not None
            assert quote.event.payload["body"] == "private"
            assert quote.event.source_transport_id == event.source_transport_id
            assert quote.event.metadata == EventMetadata()
            assert quote.event.relations == ()
        finally:
            await store._run_in_thread(store._require_db().set_authorizer, None)
    finally:
        await store.close()
