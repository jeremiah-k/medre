"""Fair ceiling-sweep catch-up behind receipted history and live streams.

Two bounded-progress properties behind the 2026-11-23 pressure
measurement, pinned at toy scale where they are cheap and deterministic:

- A newly elapsed ceiling behind a wall of already-receipted elapsed
  policies is receipted within a bounded number of sweep calls, and no
  call enforces more than its limit on a clean history.
- Admissions arriving continuously while the sweep drains are all
  receipted within a bounded number of calls, with no duplicated
  receipts: the durable cursor wraps under sustained newer arrivals
  instead of starving them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.ceiling_audit import _admit_with_ceiling, _read_ceiling


async def _backdate(store: SQLiteStorage, event_id: str, seconds: int = 5) -> None:
    """Backdate one admitted event's ceiling so it is elapsed for sweeps."""
    elapsed = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await store._write(
        "UPDATE event_content_access_policies SET hard_ceiling=? WHERE event_id=?",
        (elapsed.isoformat(timespec="microseconds"), event_id),
    )


async def _receipted(store: SQLiteStorage, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT 1 FROM event_content_ceiling_log WHERE event_id=?", (event_id,)
    )
    return row is not None


async def _receipt_count(store: SQLiteStorage) -> int:
    row = await store._read_one(
        "SELECT COUNT(*) AS n FROM event_content_ceiling_log", ()
    )
    assert row is not None
    return int(row["n"])


async def _sweep_now(store: SQLiteStorage) -> datetime:
    ceilings = await store._read_all(
        "SELECT hard_ceiling FROM event_content_access_policies", ()
    )
    return max(datetime.fromisoformat(str(row["hard_ceiling"])) for row in ceilings) + (
        timedelta(seconds=1)
    )


async def test_newcomer_behind_receipted_history_is_receipted_within_bounded_calls(
    temp_storage: SQLiteStorage,
) -> None:
    history = [f"fair-history-{index:03}" for index in range(40)]
    for event_id in history:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _sweep_now(temp_storage)
    assert len(await temp_storage.enforce_content_ceilings(now=now, limit=100)) == 40

    newcomers = ["fair-newcomer-a", "fair-newcomer-b", "fair-newcomer-c"]
    for event_id in newcomers:
        await _admit_with_ceiling(temp_storage, event_id)
        await _backdate(temp_storage, event_id)
    newcomer_ceilings = [
        await _read_ceiling(temp_storage, event_id) for event_id in newcomers
    ]
    # Both the already-receipted history and the newcomers must be elapsed.
    now = max(await _sweep_now(temp_storage), max(newcomer_ceilings)) + timedelta(
        seconds=1
    )
    for event_id in history:
        assert await _read_ceiling(temp_storage, event_id) <= now

    enforced_per_call: list[int] = []
    receipted = False
    for _ in range(6):
        enforced = await temp_storage.enforce_content_ceilings(now=now, limit=10)
        enforced_per_call.append(len(enforced))
        assert len(enforced) <= 10
        newcomer_receipts = [
            await _receipted(temp_storage, event_id) for event_id in newcomers
        ]
        if all(newcomer_receipts):
            receipted = True
            break
    assert receipted, f"newcomers not receipted within 6 calls: {enforced_per_call}"
    assert sum(enforced_per_call) == 3
    # A further call over the clean store enforces nothing new.
    assert await temp_storage.enforce_content_ceilings(now=now, limit=10) == []


async def test_sustained_admission_stream_is_receipted_within_bounded_calls(
    temp_storage: SQLiteStorage,
) -> None:
    # History must stay eligible and sorted ahead of the newly arriving keys.
    history = [f"fair-older-{index:03}" for index in range(40)]
    for event_id in history:
        await _admit_with_ceiling(temp_storage, event_id)
    now = await _sweep_now(temp_storage)
    assert len(await temp_storage.enforce_content_ceilings(now=now, limit=100)) == 40
    baseline_receipts = await _receipt_count(temp_storage)

    rounds = 24
    newcomers = [f"fair-stream-{index:03}" for index in range(rounds)]
    progress_while_admitting = False
    enforced_per_call: list[int] = []
    for index, event_id in enumerate(newcomers):
        await _admit_with_ceiling(temp_storage, event_id)
        await _backdate(temp_storage, event_id)
        now = max(now, await _read_ceiling(temp_storage, event_id)) + timedelta(
            seconds=1
        )
        enforced = await temp_storage.enforce_content_ceilings(now=now, limit=5)
        enforced_per_call.append(len(enforced))
        assert len(enforced) <= 5
        if index < rounds - 1 and await _receipted(temp_storage, newcomers[0]):
            progress_while_admitting = True
    assert (
        progress_while_admitting
    ), f"new arrivals starved until admissions stopped: {enforced_per_call}"
    for _ in range(40):
        enforced = await temp_storage.enforce_content_ceilings(now=now, limit=5)
        assert len(enforced) <= 5
        if all([await _receipted(temp_storage, eid) for eid in newcomers]):
            break
    assert all([await _receipted(temp_storage, eid) for eid in newcomers])
    assert await _receipt_count(temp_storage) - baseline_receipts == rounds
    assert await temp_storage.enforce_content_ceilings(now=now, limit=25) == []
