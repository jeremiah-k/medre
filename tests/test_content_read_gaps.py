"""Failure arms of the scoped content-read surface fail closed or reject input."""

from datetime import datetime, timedelta, timezone

import msgspec
import pytest

from medre.core.engine.replay.engine import ReplayEngine
from medre.core.engine.replay.types import ReplayRequest
from medre.core.storage.content_availability import (
    AvailabilityPolicy,
    ContentHold,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_CLOCK = datetime(2026, 10, 30, tzinfo=timezone.utc)


async def test_scoped_reads_reject_blank_and_non_string_event_ids(temp_storage):
    for bad in ("", None, 7):
        with pytest.raises(ValueError, match="An exact event ID is required"):
            await temp_storage.read_reply_context(bad)
        with pytest.raises(ValueError, match="An exact event ID is required"):
            await temp_storage.read_event_for_replay(bad)


async def test_native_submission_fence_rejects_unusable_worker_and_attempt(
    temp_storage,
):
    for bad_worker in ("", None):
        with pytest.raises(ValueError, match="concrete outbox worker"):
            await temp_storage.authorize_native_submission(
                "evt", "obox", worker_id=bad_worker, attempt_number=1
            )
    for bad_attempt in (0, -1, True, "2"):
        with pytest.raises(ValueError, match="positive reserved attempt number"):
            await temp_storage.authorize_native_submission(
                "evt", "obox", worker_id="worker-1", attempt_number=bad_attempt
            )


async def test_outbox_body_read_denies_an_unknown_generation(temp_storage):
    event = msgspec.structs.replace(
        make_storage_event(event_id="owned"), payload={"body": "retained"}
    )
    await temp_storage.append(event)
    read = await temp_storage.read_body_for_outbox("owned", "obox-never")
    assert (read.status, read.body) == ("denied", None)


async def test_optional_body_read_denies_when_the_source_hold_is_released(
    tmp_path,
):
    store = SQLiteStorage(str(tmp_path / "holds.db"))
    await store.initialize()
    try:
        event = msgspec.structs.replace(
            make_storage_event(event_id="present"), payload={"body": "secret"}
        )
        await store.append(event)
        policy = AvailabilityPolicy(
            hard_ceiling=_CLOCK + timedelta(hours=1),
            max_optional_hold=timedelta(minutes=20),
            allowed_optional_consumers=frozenset({"reply_quotes"}),
        )
        hold = ContentHold(
            "reply_quotes", "body", _CLOCK + timedelta(minutes=10), issued_at=_CLOCK
        )
        await store.issue_optional_body_hold("present", hold, policy, now=_CLOCK)
        # Out-of-band release of the admission hold: an optional consumer
        # grant must not keep retained bytes readable once the source owner
        # is gone.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE event_id='present' AND owner_kind IN ('ingress','append')",
            (_CLOCK.isoformat(timespec="microseconds"),),
        )
        read = await store.read_body_for_consumer("present", "reply_quotes", now=_CLOCK)
        assert (read.status, read.body) == ("denied", None)
    finally:
        await store.close()


async def test_replay_count_matching_counts_missing_ids_and_queries(temp_storage):
    engine = ReplayEngine(temp_storage)
    assert (
        await engine.count_matching(ReplayRequest(correlation_ids=["never-stored"]))
        == 1
    )
    await temp_storage.append(
        msgspec.structs.replace(
            make_storage_event(event_id="counted"), payload={"body": "x"}
        )
    )
    assert await engine.count_matching(ReplayRequest()) == 1
    assert (
        await engine.count_matching(
            ReplayRequest(correlation_ids=["counted", "absent"])
        )
        == 2
    )


async def test_scoped_reads_sample_their_own_clocks_and_validate_identities(
    temp_storage,
):
    """Production calls omit now; ids are validated before any grant work."""
    from medre.core.storage.sqlite._projections import inspection_sql

    event = msgspec.structs.replace(
        make_storage_event(event_id="clocked"), payload={"body": "retained"}
    )
    await temp_storage.append(event)
    # No injected clock: each read samples UTC inside its own transaction.
    assert (await temp_storage.read_body_for_outbox("clocked", "obox-x")).status == (
        "denied"
    )
    assert (
        await temp_storage.read_body_for_outbox(
            "clocked", "obox-x", now=datetime.now(timezone.utc)
        )
    ).status == "denied"
    assert (await temp_storage.read_body_for_consumer("clocked", "quotes")).status == (
        "denied"
    )
    assert (await temp_storage.read_reply_context("clocked")).status == "retained"
    with pytest.raises(ValueError, match="event and outbox identity"):
        await temp_storage.authorize_native_submission(
            "", "obox", worker_id="worker-1", attempt_number=1
        )
    with pytest.raises(ValueError, match="Inspection requires a schema-owned"):
        inspection_sql(
            "SELECT one_column FROM delivery_receipts", "delivery_receipts", True
        )
