"""Bounded ceiling sweep cursors must show progress on the next poll.

Two step-level properties of the sweep's durable cursors:

- After a poll enforces the first of two pending policies, the very next
  poll must reach the second policy. A cursor that resets after an
  enforcement rescans the already-receipted history and starves the
  second event past any bounded poll count.
- The residual repair queue keeps its saved upper bound while a range is
  in progress: newcomer receipts appended behind the saved bound are not
  scanned until the old range finishes and wraps. One poll finishes the
  old range, the next poll repairs the first receipt.
"""

from __future__ import annotations

import json
from datetime import timedelta

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _admit_with_ceiling, _read_ceiling

_RESIDUAL_CURSOR_KEY = "content_ceiling_residual_cursor"


async def _stored_residual_cursor(store: SQLiteStorage) -> list[str]:
    row = await store._read_one(
        "SELECT value FROM _medre_schema_meta WHERE key=?",
        (_RESIDUAL_CURSOR_KEY,),
    )
    assert row is not None
    saved = json.loads(str(row["value"]))
    assert isinstance(saved, list) and len(saved) == 2
    return [str(value) for value in saved]


async def test_next_poll_after_first_enforcement_reaches_the_second_event(
    temp_storage: SQLiteStorage,
) -> None:
    prior = [f"ceiling-step-prior-{index:02}" for index in range(25)]
    for event_id in prior:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, prior[-1]) + timedelta(seconds=1)
    assert len(await temp_storage.enforce_content_ceilings(now=now, limit=25)) == 25
    # Wrap the policy cursor over the clean history.
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []

    pending = ["ceiling-step-first", "ceiling-step-second"]
    for event_id in pending:
        await _admit_with_ceiling(temp_storage, event_id)
    later = await _read_ceiling(temp_storage, pending[-1]) + timedelta(seconds=1)

    # The first poll spends its budget on the receipted history page.
    assert await temp_storage.enforce_content_ceilings(now=later, limit=1) == []
    # The poll right after the first enforcement must return the second
    # event, not restart the history scan.
    first = await temp_storage.enforce_content_ceilings(now=later, limit=1)
    assert [item.event_id for item in first] == [pending[0]]
    second = await temp_storage.enforce_content_ceilings(now=later, limit=1)
    assert [item.event_id for item in second] == [pending[1]]
    assert await temp_storage.enforce_content_ceilings(now=later, limit=1) == []


async def test_residual_range_finishes_before_appended_receipts_are_scanned(
    temp_storage: SQLiteStorage,
) -> None:
    ids = [f"ceiling-range-page-{index:02}" for index in range(30)]
    for event_id in ids:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _read_ceiling(temp_storage, ids[-1]) + timedelta(seconds=1)
    assert len(await temp_storage.enforce_content_ceilings(now=now, limit=50)) == 30
    await temp_storage._write(
        "INSERT OR REPLACE INTO _medre_schema_meta(key, value) VALUES (?, ?)",
        (_RESIDUAL_CURSOR_KEY, f'["{ids[0]}","{ids[-1]}"]'),
    )
    # One clean scan page starts the bounded cycle over the existing history
    # and advances the cursor past the first budget of receipted rows.
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    started = await _stored_residual_cursor(temp_storage)
    assert started == ["ceiling-range-page-25", ids[-1]]

    # Newcomers obtain real receipts behind the saved upper bound.
    newcomers = [f"ceiling-range-z-{index:02}" for index in range(25)]
    for event_id in newcomers:
        await _admit_with_ceiling(temp_storage, event_id)
    newcomer_ceilings = [
        await _read_ceiling(temp_storage, event_id) for event_id in newcomers
    ]
    new_now = max(newcomer_ceilings) + timedelta(seconds=1)
    enforced = []
    for _ in range(5):
        enforced += await temp_storage.enforce_content_ceilings(now=new_now, limit=50)
        if {item.event_id for item in enforced} >= set(newcomers):
            break
    assert {item.event_id for item in enforced} >= set(newcomers)

    # Model the in-progress older scan after the newcomers arrived, then
    # land a late residual write on the first old receipt.
    await temp_storage._write(
        "UPDATE _medre_schema_meta SET value=? WHERE key=?",
        (f'["{started[0]}","{started[1]}"]', _RESIDUAL_CURSOR_KEY),
    )
    await temp_storage._write(
        'UPDATE canonical_events SET metadata=\'{"late":"text"}\' WHERE event_id=?',
        (ids[0],),
    )

    # Step one: the first poll finishes the saved old range only — no
    # newcomer receipt is scanned behind the saved upper bound, and the
    # exhausted range resets for the next wrap.
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
    assert await _stored_residual_cursor(temp_storage) == ["", ""]

    # Step two: the wrapped scan repairs the first receipt.
    repaired = await temp_storage.enforce_content_ceilings(now=now, limit=1)
    assert [item.event_id for item in repaired] == [ids[0]]
    assert repaired[0].residual_pass is True
    assert await temp_storage.enforce_content_ceilings(now=now, limit=1) == []
