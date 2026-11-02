"""Admission-ordered bounded candidate discovery: paging, fairness, cost.

Covers the retention candidate selector's keyset contract — the stable
``(created_at, event_id)`` index ordering over deliberately
non-chronological event IDs, the strict watermark lower bound, bounded
pages, caller-driven wraparound, cursor and limit validation, and the
non-advancing cursor refusal — plus the fairness and cost gates: a
synthetic mix of thousands of eligible, permanently blocked,
already-collected, and empty-body events pages through the epoch scope
with per-event outcomes never affecting the page advance, per-page cost
that does not grow with table position, and the new
``idx_events_admission_order`` index pinned by EXPLAIN QUERY PLAN.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.storage.backend import StorageBackend, StorageError
from medre.core.storage.content_retention import (
    CandidatePage,
    CollectionCandidateSource,
)
from medre.core.storage.sqlite._retention import (
    _CANDIDATE_KEYSET_SQL,
    _CANDIDATE_ORDER_SQL,
    _CANDIDATE_SELECT_SQL,
    _CANDIDATE_WATERMARK_SQL,
    sync_page_collection_candidates,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

_SCOPE_HEAD = "2026-11-02T10:00:00.000000+00:00"
_SETTLED_AT = "2026-10-31T01:00:00+00:00"

_TOTAL_SCOPE = 2500
_PRE_ACTIVATION = 500


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _scope_created_at(index: int) -> str:
    """Distinct admission times one microsecond apart inside the epoch."""
    return f"2026-11-02T10:00:00.{index:06d}+00:00"


def _scope_event_id(index: int, total: int) -> str:
    """Deliberately non-chronological event ID for admission index ``index``.

    7919 is coprime with the 2500-event scope, so ID order is a fixed
    permutation that disagrees with admission order everywhere.
    """
    return f"ev-{(index * 7919) % total:06d}"


async def _append_event_at(
    store: SQLiteStorage,
    event_id: str,
    created_at: str,
    *,
    body: str | None,
) -> None:
    """Append one event and pin its local admission time deterministically.

    Pinning ``created_at`` avoids racing the wall clock's microsecond
    resolution when a test depends on admission ordering.
    """
    payload = {"body": f"DO-NOT-DELETE-{event_id}"} if body is not None else {}
    event = msgspec.structs.replace(
        make_storage_event(event_id=event_id), payload=payload, metadata={}
    )
    await store.append(event)
    await store._write(
        "UPDATE canonical_events SET created_at=? WHERE event_id=?",
        (created_at, event_id),
    )


async def _settle_append_owner(store: SQLiteStorage, event_id: str) -> None:
    await store._write(
        "UPDATE event_content_dependencies SET released_at=? "
        "WHERE event_id=? AND owner_kind='append'",
        (_SETTLED_AT, event_id),
    )


async def _add_permanent_body_grant(store: SQLiteStorage, event_id: str) -> None:
    """One unrevoked, unexpired optional body grant: a live collector blocker."""
    await store._write(
        "INSERT INTO event_content_access_policies "
        "(event_id, version, hard_ceiling, allowed_consumers, "
        "max_optional_hold_microseconds, created_at) "
        "VALUES (?, 1, '2126-01-01T00:00:00.000000+00:00', "
        "'[\"held-consumer\"]', 1000000000000, '2026-11-01T00:00:00.000000+00:00')",
        (event_id,),
    )
    await store._write(
        "INSERT INTO event_content_access_grants "
        "(event_id, scope, consumer, issued_at, expires_at, revoked_at, expired_at) "
        "VALUES (?, 'body', 'held-consumer', '2026-11-01T00:00:00.000000+00:00', "
        "'2126-01-01T00:00:00.000000+00:00', NULL, NULL)",
        (event_id,),
    )


async def _seed_scope_event(store: SQLiteStorage, index: int, total: int) -> str:
    """Append one scope event whose outcome class varies with ``index``.

    The mix exercises the fairness property: eligible text (``1-5``),
    permanently blocked via an unsettled append owner (``6-7``),
    already-collected (``0``), empty-body (``8``), and grant-held (``9``)
    events must all page identically. Settlement and collection run in bulk
    after the whole scope is admitted.
    """
    event_id = _scope_event_id(index, total)
    outcome = index % 10
    await _append_event_at(
        store,
        event_id,
        _scope_created_at(index),
        body=None if outcome == 8 else "text",
    )
    return event_id


async def _build_mixed_store(path: str) -> None:
    """Build the mixed-outcome store: thousands of events, shuffled IDs.

    Pre-activation history (500 events) is admitted before the epoch opens;
    the 2500 epoch-scoped events mix eligible, permanently blocked,
    already-collected, empty-body, and grant-held outcomes with event IDs
    that disagree with admission order everywhere.
    """
    store = SQLiteStorage(path)
    await store.initialize()
    try:
        for index in range(_PRE_ACTIVATION):
            await _append_event_at(
                store,
                f"pre-{index:05d}",
                f"2026-11-01T10:00:00.{index:06d}+00:00",
                body="text",
            )
        epoch = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 2)
        )
        assert epoch.admission_watermark == (
            f"2026-11-01T10:00:00.{_PRE_ACTIVATION - 1:06d}+00:00"
        )
        for index in range(_TOTAL_SCOPE):
            await _seed_scope_event(store, index, _TOTAL_SCOPE)
        # Settle every append owner in one statement, then re-open the
        # permanently blocked ones; the empty-body events keep their
        # unsettled-admission shape, and the collected ones are collected
        # through the real guarded collector.
        await store._write(
            "UPDATE event_content_dependencies SET released_at=? "
            "WHERE owner_kind='append'",
            (_SETTLED_AT,),
        )
        blocked_ids = [
            _scope_event_id(index, _TOTAL_SCOPE)
            for index in range(_TOTAL_SCOPE)
            if index % 10 in (6, 7)
        ]
        placeholders = ",".join("?" for _ in blocked_ids)
        await store._write(
            "UPDATE event_content_dependencies SET released_at=NULL "
            "WHERE owner_kind='append' AND event_id IN "
            f"({placeholders})",  # nosec B608 - closed event ID list, all bound.
            tuple(blocked_ids),
        )
        for index in range(0, _TOTAL_SCOPE, 10):
            collected_id = _scope_event_id(index, _TOTAL_SCOPE)
            assert (await store.collect_event_content(collected_id, dry_run=False))[
                "status"
            ] == "collected"
        for index in range(9, _TOTAL_SCOPE, 10):
            await _add_permanent_body_grant(store, _scope_event_id(index, _TOTAL_SCOPE))
    finally:
        await store.close()


def _mixed_store_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = str(tmp_path_factory.mktemp("paging") / "mix.db")
    asyncio.run(_build_mixed_store(path))
    return path


@pytest.fixture(scope="module")
def mixed_store_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    return _mixed_store_path(tmp_path_factory)


async def _open(path: str) -> SQLiteStorage:
    store = SQLiteStorage(path)
    await store.initialize()
    return store


async def _page_all(
    store: SQLiteStorage,
    *,
    watermark: str | None,
    limit: int = 100,
) -> list[str]:
    """Page the whole epoch scope to its empty terminator; return all IDs."""
    seen: list[str] = []
    after: tuple[str, str] | None = None
    while True:
        page = await store.page_collection_candidates(
            after=after, limit=limit, watermark=watermark
        )
        assert len(page.event_ids) <= limit
        if not page.event_ids:
            assert page.has_more is False
            assert page.next_after is None
            return seen
        seen.extend(page.event_ids)
        assert page.next_after is not None
        after = page.next_after


# ---------------------------------------------------------------------------
# Fairness: full-scope paging over the mixed outcome classes
# ---------------------------------------------------------------------------


async def test_full_epoch_scope_visits_every_mixed_outcome_event_exactly_once(
    mixed_store_path: str,
) -> None:
    store = await _open(mixed_store_path)
    try:
        watermark = (await store.latest_retention_epoch()).admission_watermark
        assert watermark is not None
        seen = await _page_all(store, watermark=watermark, limit=100)
        expected = [
            _scope_event_id(index, _TOTAL_SCOPE) for index in range(_TOTAL_SCOPE)
        ]
        # Every epoch-scoped event exactly once per cycle, regardless of the
        # per-event outcome (blocked, collected, empty, and grant-held
        # events included); pre-activation history never appears.
        assert len(seen) == _TOTAL_SCOPE
        assert sorted(seen) == sorted(expected)
        assert len(set(seen)) == _TOTAL_SCOPE
        assert not any(event_id.startswith("pre-") for event_id in seen)
        # The admission ordering is the (created_at, event_id) key, not the
        # ID order, and memory stays bounded by the page limit.
        assert seen == expected
    finally:
        await store.close()


async def test_second_sweep_cycle_restarts_from_the_scope_head(
    mixed_store_path: str,
) -> None:
    store = await _open(mixed_store_path)
    try:
        watermark = (await store.latest_retention_epoch()).admission_watermark
        assert watermark is not None
        first = await _page_all(store, watermark=watermark, limit=100)
        # A new cycle restarts with after=None and re-visits the whole
        # scope: permanently blocked events never stall a later cycle.
        second = await _page_all(store, watermark=watermark, limit=100)
        assert first == second
    finally:
        await store.close()


async def test_blocked_first_event_never_starves_later_candidates(tmp_path) -> None:
    """The core fairness property in isolation.

    The chronologically first epoch-scoped event is permanently blocked; a
    bounded page still advances past it and later candidates are returned.
    """
    store = SQLiteStorage(str(tmp_path / "starvation.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 2))
        await _append_event_at(
            store, "blocker", _SCOPE_HEAD, body="text"
        )  # unsettled append owner: permanently blocked in this fixture
        for index in range(1, 8):
            event_id = f"later-{index}"
            await _append_event_at(
                store, event_id, _scope_created_at(index), body="text"
            )
            await _settle_append_owner(store, event_id)
        seen = await _page_all(store, watermark="2026-11-02T09:00:00+00:00", limit=3)
        assert seen == ["blocker"] + [f"later-{index}" for index in range(1, 8)]
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Keyset correctness
# ---------------------------------------------------------------------------


async def test_ties_on_created_at_break_by_event_id(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "ties.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 2))
        same_instant = _SCOPE_HEAD
        # Insert deliberately out of ID order; the tie order is the
        # lexicographic event ID order.
        for event_id in ("tie-mid", "tie-last", "tie-first", "tie-early"):
            await _append_event_at(store, event_id, same_instant, body="text")
            await _settle_append_owner(store, event_id)
        watermark = "2026-11-02T09:00:00+00:00"
        page = await store.page_collection_candidates(
            after=None, limit=3, watermark=watermark
        )
        assert page.event_ids == ["tie-early", "tie-first", "tie-last"]
        assert page.has_more is True
        assert page.next_after == (same_instant, "tie-last")
        rest = await store.page_collection_candidates(
            after=page.next_after, limit=3, watermark=watermark
        )
        assert rest.event_ids == ["tie-mid"]
        assert rest.has_more is False
        assert rest.next_after == (same_instant, "tie-mid")
    finally:
        await store.close()


async def test_watermark_lower_bound_is_strict_across_mixed_text_forms(
    tmp_path,
) -> None:
    """An event at the watermark instant is never a candidate.

    The admission ``created_at`` text omits the fraction only when the
    microsecond is zero, so the boundary is pinned across both renderings
    of the same instant.
    """
    store = SQLiteStorage(str(tmp_path / "boundary.db"))
    await store.initialize()
    try:
        # The watermark-defining event carries the zero-microsecond
        # long-form rendering of its instant.
        await _append_event_at(store, "at-watermark", _SCOPE_HEAD, body="text")
        epoch = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 2)
        )
        assert epoch.admission_watermark == _SCOPE_HEAD
        # The same instant in the short rendering sorts before the
        # watermark text and is excluded with it.
        await _append_event_at(
            store, "at-instant-short", "2026-11-02T10:00:00+00:00", body="text"
        )
        await _append_event_at(
            store,
            "one-microsecond-after",
            "2026-11-02T10:00:00.000001+00:00",
            body="text",
        )
        seen = await _page_all(store, watermark=epoch.admission_watermark, limit=100)
        assert seen == ["one-microsecond-after"]
    finally:
        await store.close()


async def test_null_watermark_matches_empty_store_epoch_semantics(tmp_path) -> None:
    """A None watermark applies no filter, exactly like the aggregate preview.

    The activation-epoch contract: a store that held no events at activation
    records a NULL watermark, so every current event is post-activation.
    """
    store = SQLiteStorage(str(tmp_path / "null-watermark.db"))
    await store.initialize()
    try:
        epoch = await store.open_retention_epoch(
            activated_by="runtime-first-start", now=_utc(2026, 11, 2)
        )
        assert epoch.admission_watermark is None
        for index in range(5):
            event_id = f"nv-{index}"
            await _append_event_at(
                store, event_id, _scope_created_at(index), body="text"
            )
            await _settle_append_owner(store, event_id)
        seen = await _page_all(store, watermark=None, limit=2)
        assert seen == [f"nv-{index}" for index in range(5)]
    finally:
        await store.close()


async def test_empty_scope_returns_terminated_empty_page(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "empty-scope.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 2))
        page = await store.page_collection_candidates(
            after=None,
            limit=100,
            watermark="2030-01-01T00:00:00.000000+00:00",
        )
        assert page.event_ids == []
        assert page.next_after is None
        assert page.has_more is False
    finally:
        await store.close()


async def test_paging_past_end_terminates_and_restart_is_caller_driven(
    tmp_path,
) -> None:
    """Wraparound is caller-driven: past the end is an empty page.

    The final non-empty page reports ``has_more=False`` with a non-None
    ``next_after``; one more request with that cursor terminates. A new
    cycle is the caller's decision and restarts with ``after=None``.
    """
    store = SQLiteStorage(str(tmp_path / "wraparound.db"))
    await store.initialize()
    try:
        await store.open_retention_epoch(activated_by="operator", now=_utc(2026, 11, 2))
        for index in range(4):
            event_id = f"wp-{index}"
            await _append_event_at(
                store, event_id, _scope_created_at(index), body="text"
            )
            await _settle_append_owner(store, event_id)
        watermark = "2026-11-02T09:00:00+00:00"
        page = await store.page_collection_candidates(
            after=None, limit=4, watermark=watermark
        )
        assert page.event_ids == [f"wp-{index}" for index in range(4)]
        assert page.has_more is False
        assert page.next_after == (_scope_created_at(3), "wp-3")
        terminator = await store.page_collection_candidates(
            after=page.next_after, limit=4, watermark=watermark
        )
        assert terminator.event_ids == []
        assert terminator.has_more is False
        assert terminator.next_after is None
        head_restart = await store.page_collection_candidates(
            after=None, limit=4, watermark=watermark
        )
        assert head_restart.event_ids == page.event_ids
    finally:
        await store.close()


async def test_cursor_at_or_before_watermark_continues_from_boundary_head(
    tmp_path,
) -> None:
    """A stale or corrupt cursor cannot pull pre-activation events.

    A cursor at or before the activation boundary continues from the strict
    watermark head: every returned row is strictly inside the epoch scope
    and the page still advances.
    """
    store = SQLiteStorage(str(tmp_path / "stale-cursor.db"))
    await store.initialize()
    try:
        await _append_event_at(
            store, "historic", "2026-11-01T10:00:00+00:00", body="text"
        )
        epoch = await store.open_retention_epoch(
            activated_by="operator", now=_utc(2026, 11, 2)
        )
        assert epoch.admission_watermark == "2026-11-01T10:00:00+00:00"
        for index in range(3):
            event_id = f"fresh-{index}"
            await _append_event_at(
                store, event_id, _scope_created_at(index), body="text"
            )
            await _settle_append_owner(store, event_id)
        watermark = epoch.admission_watermark
        assert watermark is not None
        for stale_after in (
            ("2020-01-01T00:00:00+00:00", "ancient"),
            (watermark, "historic"),
        ):
            page = await store.page_collection_candidates(
                after=stale_after, limit=2, watermark=watermark
            )
            assert page.event_ids == ["fresh-0", "fresh-1"]
            assert page.next_after == (_scope_created_at(1), "fresh-1")
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Validation and refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, 101, "5", True, None, 2.5])
async def test_out_of_range_limit_is_refused(tmp_path, limit) -> None:
    store = SQLiteStorage(str(tmp_path / "limit.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match=r"integer in \[1, 100\]"):
            await store.page_collection_candidates(
                after=None, limit=limit, watermark=None
            )
    finally:
        await store.close()


@pytest.mark.parametrize(
    "after",
    [
        "not-a-tuple",
        ("only-one",),
        ("too", "many", "parts"),
        ("", "empty-created-at"),
        ("2026-11-02T10:00:00.000000+00:00", ""),
        ("2026-11-02T10:00:00.000000+00:00", 7),
    ],
)
async def test_malformed_cursor_is_refused(tmp_path, after) -> None:
    store = SQLiteStorage(str(tmp_path / "cursor.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match="cursor must be a non-empty"):
            await store.page_collection_candidates(after=after, limit=5, watermark=None)
    finally:
        await store.close()


@pytest.mark.parametrize("watermark", ["", 7, b"2026-11-02"])
async def test_malformed_watermark_is_refused(tmp_path, watermark) -> None:
    store = SQLiteStorage(str(tmp_path / "watermark.db"))
    await store.initialize()
    try:
        with pytest.raises(ValueError, match="watermark must be a timestamp"):
            await store.page_collection_candidates(
                after=None, limit=5, watermark=watermark
            )
    finally:
        await store.close()


async def test_non_advancing_cursor_refuses_to_loop() -> None:
    """A first row at or before the cursor raises instead of looping.

    The strict keyset predicate makes this unreachable through well-formed
    SQL; the guard pins the refusal so a future ``>=`` rewrite, a stale
    replayed cursor, or collation drift cannot silently spin a retention
    sweep on a duplicate keyset. Rows arrive in SELECT order —
    ``(event_id, created_at)`` — while cursors are ``(created_at,
    event_id)``.
    """
    head_row = ("ev-1", "2026-11-02T10:00:00.000000+00:00")

    class _StubCursor:
        def __init__(self, rows: list[tuple[str, str]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[str, str]]:
            return self._rows

    class _StubDB:
        def __init__(self, rows: list[tuple[str, str]]) -> None:
            self._rows = rows

        def execute(self, sql: str, params: tuple[object, ...]) -> _StubCursor:
            return _StubCursor(self._rows)

    # A cursor injected as the duplicate of the returned first row.
    with pytest.raises(RuntimeError, match="did not advance its keyset"):
        sync_page_collection_candidates(
            _StubDB([head_row]),  # type: ignore[arg-type]
            threading.Lock(),
            after=(head_row[1], head_row[0]),
            limit=5,
            watermark=None,
        )
    # A cursor injected ahead of everything the storage returns.
    with pytest.raises(RuntimeError, match="did not advance its keyset"):
        sync_page_collection_candidates(
            _StubDB([head_row]),  # type: ignore[arg-type]
            threading.Lock(),
            after=("2026-11-02T11:00:00.000000+00:00", "ev-9"),
            limit=5,
            watermark=None,
        )


async def test_broken_storage_surfaces_as_storage_error(tmp_path) -> None:
    store = SQLiteStorage(str(tmp_path / "broken.db"))
    await store.initialize()
    try:
        await store._write("DROP TABLE canonical_events")
        with pytest.raises(StorageError, match="candidate paging failed"):
            await store.page_collection_candidates(after=None, limit=5, watermark=None)
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Contract seams
# ---------------------------------------------------------------------------


async def test_selector_satisfies_collection_candidate_source_protocol(
    tmp_path,
) -> None:
    store = SQLiteStorage(str(tmp_path / "protocol.db"))
    await store.initialize()
    try:
        # Static structural conformance: the runtime retention worker
        # consumes this protocol without depending on SQLite.
        source: CollectionCandidateSource = store
        page = await source.page_collection_candidates(
            after=None, limit=10, watermark=None
        )
        assert isinstance(page, CandidatePage)
        # The concrete selector stays off the StorageBackend protocol.
        assert not hasattr(StorageBackend, "page_collection_candidates")
    finally:
        await store.close()


async def test_explain_query_plan_pins_the_admission_order_index(tmp_path) -> None:
    """Every selector query shape is index-driven on the allowlisted index.

    Pinning the plan keeps the ordering index-driven for realistic sizes:
    nobody can silently drop ``idx_events_admission_order`` and turn the
    bounded page into a sort.
    """
    store = SQLiteStorage(str(tmp_path / "explain.db"))
    await store.initialize()
    try:
        head = _CANDIDATE_SELECT_SQL + _CANDIDATE_ORDER_SQL
        watermark = (
            _CANDIDATE_SELECT_SQL + _CANDIDATE_WATERMARK_SQL + _CANDIDATE_ORDER_SQL
        )
        keyset = _CANDIDATE_SELECT_SQL + _CANDIDATE_KEYSET_SQL + _CANDIDATE_ORDER_SQL
        head_plan = await store._read_all("EXPLAIN QUERY PLAN " + head, (11,))
        assert "COVERING INDEX idx_events_admission_order" in head_plan[0]["detail"]
        for sql, params in (
            (watermark, ("2026-11-02T10:00:00.000000+00:00", 11)),
            (keyset, ("2026-11-02T10:00:00.000000+00:00", "ev-0", 11)),
        ):
            plan = await store._read_all("EXPLAIN QUERY PLAN " + sql, params)
            detail = plan[0]["detail"]
            assert detail.startswith("SEARCH")
            assert "COVERING INDEX idx_events_admission_order" in detail
    finally:
        await store.close()


async def test_late_page_cost_is_bounded_not_table_proportional(
    mixed_store_path: str,
) -> None:
    """A late keyset page does not examine earlier rows.

    Measured with a SQLite progress handler over the real connection: the
    seek-based keyset page near the scope tail costs a small constant, not
    a share of the table; the full head scan costs orders of magnitude
    more. Combined with the EXPLAIN pin above, this bounds per-page work
    independently of table size.
    """
    store = await _open(mixed_store_path)
    try:
        db = store._require_db()
        watermark = (await store.latest_retention_epoch()).admission_watermark
        assert watermark is not None
        # The keyset cursor of the last full page, analytically: admission
        # time and event ID of scope index 2399.
        after = (_scope_created_at(2399), _scope_event_id(2399, _TOTAL_SCOPE))
        keyset_sql = (
            _CANDIDATE_SELECT_SQL + _CANDIDATE_KEYSET_SQL + _CANDIDATE_ORDER_SQL
        )
        steps: dict[str, int] = {"n": 0}

        def _count() -> int:
            steps["n"] += 1
            return 0

        def measure():
            with store._lock:
                db.set_progress_handler(_count, 10)
                try:
                    late_page = db.execute(keyset_sql, (*after, 101)).fetchall()
                    late_steps = steps["n"]
                    steps["n"] = 0
                    full_scope = db.execute(
                        _CANDIDATE_SELECT_SQL
                        + _CANDIDATE_WATERMARK_SQL
                        + _CANDIDATE_ORDER_SQL,
                        (watermark, _TOTAL_SCOPE + 1),
                    ).fetchall()
                    full_steps = steps["n"]
                    return late_page, late_steps, full_scope, full_steps
                finally:
                    db.set_progress_handler(None, 0)

        late_page, late_steps, full_scope, full_steps = await store._run_in_thread(
            measure
        )
        assert len(late_page) == 100
        assert len(full_scope) == _TOTAL_SCOPE
        assert late_steps < full_steps // 4
    finally:
        await store.close()
