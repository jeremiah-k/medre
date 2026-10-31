"""Opt-in stored content hold and fail-closed read semantics."""

from datetime import datetime, timedelta, timezone, tzinfo

import msgspec
import pytest

from medre.core.storage.content_availability import (
    AvailabilityPolicy,
    ContentHold,
    preview_content_access,
)
from medre.core.storage.sqlite._content_grants import ContentBodyRead
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event


@pytest.fixture
def clock():
    return datetime(2026, 10, 30, tzinfo=timezone.utc)


async def test_optional_content_read_requires_live_grant(tmp_path, clock):
    path = str(tmp_path / "state.db")
    store = SQLiteStorage(path)
    await store.initialize()
    event = msgspec.structs.replace(
        make_storage_event(event_id="present"), payload={"body": ""}
    )
    await store.append(event)
    policy = AvailabilityPolicy(
        hard_ceiling=clock + timedelta(hours=1),
        max_optional_hold=timedelta(minutes=20),
        allowed_optional_consumers=frozenset({"reply_quotes"}),
    )
    hold = ContentHold(
        "reply_quotes", "body", clock + timedelta(minutes=10), issued_at=clock
    )
    assert (
        await store.read_body_for_consumer("present", "reply_quotes", now=clock)
    ).status == "denied"
    await store.issue_optional_body_hold("present", hold, policy, now=clock)
    assert (
        await store.read_body_for_consumer("present", "other", now=clock)
    ).status == "denied"
    read = await store.read_body_for_consumer("present", "reply_quotes", now=clock)
    assert (read.status, read.body) == ("retained", "")
    assert (
        await store.read_body_for_consumer(
            "present", "reply_quotes", now=clock + timedelta(minutes=10)
        )
    ).status == "denied"
    await store.close()
    reopened = SQLiteStorage(path)
    await reopened.initialize()
    # Once expiry is observed, a backwards clock adjustment cannot reopen it.
    assert (
        await reopened.read_body_for_consumer("present", "reply_quotes", now=clock)
    ).status == "denied"
    assert not await reopened.revoke_optional_body_hold(
        "present", "reply_quotes", now=clock
    )
    assert not await reopened.revoke_optional_body_hold(
        "present", "reply_quotes", now=clock
    )
    assert (
        await reopened.read_body_for_consumer("present", "reply_quotes", now=clock)
    ).status == "denied"
    with pytest.raises(ValueError, match="resurrected"):
        await reopened.issue_optional_body_hold("present", hold, policy, now=clock)
    await reopened.close()


async def test_revocation_after_unobserved_expiry_keeps_the_expired_tombstone(
    tmp_path, clock
):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    event = msgspec.structs.replace(
        make_storage_event(event_id="lapsed"), payload={"body": ""}
    )
    await store.append(event)
    policy = AvailabilityPolicy(
        hard_ceiling=clock + timedelta(hours=1),
        max_optional_hold=timedelta(minutes=20),
        allowed_optional_consumers=frozenset({"reply_quotes"}),
    )
    hold = ContentHold(
        "reply_quotes", "body", clock + timedelta(minutes=10), issued_at=clock
    )
    await store.issue_optional_body_hold("lapsed", hold, policy, now=clock)
    # No read or issue call observes the deadline first: revocation must not
    # convert the lapsed grant into a revoked one regardless of call order.
    late = clock + timedelta(minutes=11)
    assert not await store.revoke_optional_body_hold("lapsed", "reply_quotes", now=late)
    row = await store._read_one(
        "SELECT revoked_at, expired_at FROM event_content_access_grants "
        "WHERE event_id='lapsed' AND scope='body' AND consumer='reply_quotes'"
    )
    assert row is not None
    assert row["revoked_at"] is None
    assert row["expired_at"] is not None
    assert (
        await store.read_body_for_consumer("lapsed", "reply_quotes", now=clock)
    ).status == "denied"
    await store.close()


async def test_immutable_ceiling_and_denied_unknown_event(tmp_path, clock):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    policy = AvailabilityPolicy(
        hard_ceiling=clock + timedelta(hours=1),
        max_optional_hold=timedelta(minutes=10),
        allowed_optional_consumers=frozenset({"reply_quotes", "history"}),
    )
    hold = ContentHold(
        "reply_quotes", "body", clock + timedelta(minutes=10), issued_at=clock
    )
    with pytest.raises(ValueError, match="admitted"):
        await store.issue_optional_body_hold("missing", hold, policy, now=clock)
    assert (
        await store.read_body_for_consumer("missing", "reply_quotes", now=clock)
    ).status == "denied"
    await store.append(
        msgspec.structs.replace(make_storage_event(event_id="no-body"), payload={})
    )
    await store.issue_optional_body_hold("no-body", hold, policy, now=clock)
    assert (
        await store.read_body_for_consumer("no-body", "reply_quotes", now=clock)
    ).status == "unavailable"
    changed = AvailabilityPolicy(
        hard_ceiling=clock + timedelta(hours=2),
        max_optional_hold=timedelta(minutes=10),
        allowed_optional_consumers=policy.allowed_optional_consumers,
    )
    with pytest.raises(ValueError, match="immutable"):
        await store.issue_optional_body_hold(
            "no-body",
            ContentHold(
                "history", "body", clock + timedelta(minutes=5), issued_at=clock
            ),
            changed,
            now=clock,
        )
    with pytest.raises(ValueError, match="bounded"):
        await store.issue_optional_body_hold(
            "no-body",
            ContentHold(
                "history", "body", clock + timedelta(minutes=15), issued_at=clock
            ),
            policy,
            now=clock,
        )
    assert (
        await store.read_body_for_consumer("no-body", "history", now=clock)
    ).status == "denied"
    await store.close()


async def test_exact_live_ceiling_is_enforced(tmp_path, clock):
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    await store.append(
        msgspec.structs.replace(
            make_storage_event(event_id="body"), payload={"body": "private"}
        )
    )
    policy = AvailabilityPolicy(
        clock + timedelta(minutes=10), timedelta(minutes=10), frozenset({"history"})
    )
    hold = ContentHold(
        "history", "body", clock + timedelta(minutes=10), issued_at=clock
    )
    await store.issue_optional_body_hold("body", hold, policy, now=clock)
    assert (
        await store.read_body_for_consumer(
            "body", "history", now=clock + timedelta(minutes=9)
        )
    ).body == "private"
    assert (
        await store.read_body_for_consumer(
            "body", "history", now=clock + timedelta(minutes=10)
        )
    ).status == "denied"
    await store.close()


def test_content_read_result_refuses_invalid_status_and_leaked_bytes():
    with pytest.raises(ValueError, match="Unknown"):
        ContentBodyRead("invented")
    with pytest.raises(ValueError, match="Only retained"):
        ContentBodyRead("denied", "private")


@pytest.mark.parametrize("status", ["denied", "unavailable"])
@pytest.mark.parametrize("body", [b"private", 1, {}, []])
def test_non_retained_read_requires_absent_body(status, body):
    with pytest.raises(ValueError, match="Only retained"):
        ContentBodyRead(status, body)


async def test_hold_duration_is_elapsed_time_across_dst_fold(tmp_path):
    class FoldOffset(tzinfo):
        # Deterministic DST fold without a platform timezone-data dependency.
        def utcoffset(self, dt):
            return timedelta(hours=-6 if dt.fold else -5)

        def dst(self, dt):
            return timedelta(0)

    zone = FoldOffset()
    issued = datetime(2026, 11, 1, 0, 30, tzinfo=zone)
    expiry = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    policy = AvailabilityPolicy(
        datetime(2026, 11, 1, 3, tzinfo=zone),
        timedelta(hours=1),
        frozenset({"history"}),
    )
    store = SQLiteStorage(str(tmp_path / "state.db"))
    await store.initialize()
    try:
        await store.append(make_storage_event(event_id="dst"))
        hold = ContentHold("history", "body", expiry, issued_at=issued)
        assert (
            preview_content_access(
                now=issued,
                scope="body",
                available=True,
                policy=policy,
                holds=(hold,),
                requester="history",
            ).result
            == "denied"
        )
        with pytest.raises(ValueError, match="bounded"):
            await store.issue_optional_body_hold(
                "dst",
                hold,
                policy,
                now=issued,
            )
        with pytest.raises(ValueError, match="precede expiry"):
            ContentHold(
                "history",
                "body",
                datetime(2026, 11, 1, 1, 50, tzinfo=zone),
                issued_at=datetime(2026, 11, 1, 1, 10, tzinfo=zone, fold=1),
            )
        assert (
            await store.read_body_for_consumer("dst", "history", now=issued)
        ).status == "denied"
    finally:
        await store.close()
