"""Explicit observed instants keep ceiling control reads deterministic.

Control reads that rely on the wall clock can cross an admitted ceiling
mid-test and fail without any regression: the body read starts denying
and the single-event audit stops reporting ``not_elapsed``. Passing an
explicit ``now`` to both surfaces pins the boundary: the same instant
before the ceiling reads retained and reports ``not_elapsed``, the same
instant after it denies and reports ``elapsed_pending``.

A prepared lifecycle database also proves that moving an admitted
policy's ``hard_ceiling`` is accepted and enforced: no trigger refuses
the update, and the sweep records the moved instant as the receipt's
ceiling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import (
    _admit_with_ceiling,
    _read_ceiling,
    _seed_every_copy,
)


async def test_control_reads_honor_an_explicit_pre_ceiling_instant(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "ceiling-explicit-instant"
    await _admit_with_ceiling(temp_storage, event_id)
    outbox_id = await _seed_every_copy(temp_storage, event_id)
    ceiling = await _read_ceiling(temp_storage, event_id)
    pre_ceiling = ceiling - timedelta(seconds=1)
    post_ceiling = ceiling + timedelta(seconds=1)

    body = await temp_storage.read_body_for_outbox(event_id, outbox_id, now=pre_ceiling)
    assert body.status == "retained"
    audit = await temp_storage.audit_content_ceilings(
        now=pre_ceiling, event_id=event_id
    )
    assert audit["state"] == "not_elapsed"
    assert audit["residual_stores_total"] > 0

    denied = await temp_storage.read_body_for_outbox(
        event_id, outbox_id, now=post_ceiling
    )
    assert denied.status == "denied"
    elapsed = await temp_storage.audit_content_ceilings(
        now=post_ceiling, event_id=event_id
    )
    assert elapsed["state"] == "elapsed_pending"
    assert elapsed["residual_stores"]["event_body_content"] == 1


async def test_policy_hard_ceiling_update_is_accepted_and_enforced(
    temp_storage: SQLiteStorage,
) -> None:
    event_id = "ceiling-policy-move"
    await _admit_with_ceiling(temp_storage, event_id)
    moved = datetime.now(timezone.utc) - timedelta(seconds=60)

    await temp_storage._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? " "WHERE event_id=?",
        (moved.isoformat(timespec="microseconds"), event_id),
    )
    row = await temp_storage._read_one(
        "SELECT hard_ceiling FROM event_content_access_policies WHERE event_id=?",
        (event_id,),
    )
    assert row is not None
    assert datetime.fromisoformat(str(row["hard_ceiling"])) == moved

    enforced = await temp_storage.enforce_content_ceilings(
        now=moved + timedelta(seconds=1)
    )
    assert [item.event_id for item in enforced] == [event_id]
    receipt = await temp_storage._read_one(
        "SELECT ceiling FROM event_content_ceiling_log WHERE event_id=?",
        (event_id,),
    )
    assert receipt is not None
    assert datetime.fromisoformat(str(receipt["ceiling"])) == moved
    verification = await temp_storage.verify_content_ceiling_receipts(event_id=event_id)
    assert verification["verdict"] == "passed"
