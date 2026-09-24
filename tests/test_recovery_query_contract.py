"""Structural contracts for bounded unresolved-delivery recovery queries."""

from __future__ import annotations

import re
import sqlite3

import pytest

from medre.core.storage.backend import MAX_RECOVERY_PAGE_LIMIT
from medre.core.storage.sqlite._recovery_query import (
    _SELECT_UNRESOLVED_DELIVERIES,
    _SELECT_UNRESOLVED_DELIVERIES_SINCE,
    _candidate_batch_limit,
    _RecoveryQueryMixin,
    _validate_page_limit,
)
from medre.core.storage.sqlite.schema import _INDEXES
from medre.core.storage.sqlite.storage import SQLiteStorage


def test_lineage_index_matches_current_outcome_grouping() -> None:
    """Replay provenance is not part of one logical delivery lineage."""
    marker = "CREATE INDEX IF NOT EXISTS idx_receipts_lineage"
    start = _INDEXES.index(marker)
    end = _INDEXES.index(";", start)
    definition = _INDEXES[start : end + 1]

    assert "COALESCE(target_channel, '')" in definition
    assert "replay_run_id" not in definition
    assert definition.rstrip().endswith("sequence);")


def _candidate_where_clause(sql: str) -> str:
    """Return the bounded candidate WHERE clause or fail structurally."""
    match = re.search(
        r"\bWHERE\b(?P<where>.*?)\bORDER BY dr\.sequence ASC LIMIT \?",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, "expected a bounded candidate WHERE clause"
    return match.group("where")


def test_recovery_scan_is_receipt_keyset_bounded() -> None:
    """Recovery pages candidates before correlated authority checks."""
    sql = _SELECT_UNRESOLVED_DELIVERIES

    assert "WITH candidate_receipts AS (" in sql
    assert "FROM delivery_receipts dr NOT INDEXED" in sql
    assert "FROM delivery_status dr" not in sql
    assert "dr.sequence > ?" in sql
    assert sql.index("LIMIT ?") < sql.index("), candidate_window AS (")
    assert sql.index("), candidate_window AS (") < sql.index("NOT EXISTS")
    assert "INDEXED BY idx_receipts_lineage" not in sql
    assert "NOT EXISTS" in sql
    assert "GROUP BY" not in sql
    assert "OFFSET" not in sql
    assert "committed_outbox.attempt_number" in sql
    assert "COALESCE(" in sql
    # replay_run_id is a selected provenance column, never a lineage
    # predicate.
    where_clause = _candidate_where_clause(sql)
    assert "replay_run_id" not in where_clause


def test_since_scope_filters_the_same_event_lineage() -> None:
    """Canonical event time scopes candidates without changing lineage identity."""
    sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE

    assert "JOIN canonical_events ce ON ce.event_id = dr.event_id" in sql
    assert "ce.timestamp >= ?" in sql
    assert "FROM delivery_receipts dr NOT INDEXED" in sql
    where_clause = _candidate_where_clause(sql)
    assert "replay_run_id" not in where_clause


async def test_readonly_recovery_does_not_require_lineage_index(tmp_path) -> None:
    """Read-only recovery stays valid before the optional lineage index exists."""
    db_path = tmp_path / "recovery-no-lineage-index.db"
    storage = SQLiteStorage(str(db_path))
    await storage.initialize()
    await storage.close()

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("DROP INDEX idx_receipts_lineage")
        raw.commit()
    finally:
        raw.close()

    readonly = await SQLiteStorage.open_readonly(str(db_path))
    try:
        page = await readonly.query_unresolved_deliveries(limit=3)
        assert page.items == []
        assert page.has_more is False
    finally:
        await readonly.close()


class _RecoveryReadStub(_RecoveryQueryMixin):
    """Minimal read seam for recovery keyset control-flow tests."""

    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self._responses = list(responses)

    async def _read_all(
        self,
        _sql: str,
        _params: tuple[object, ...],
    ) -> list[dict[str, object]]:
        return self._responses.pop(0)


async def test_recovery_scan_empty_candidate_window_returns_empty_page() -> None:
    """An empty raw keyset window terminates without inventing a cursor."""
    storage = _RecoveryReadStub([[]])

    page = await storage.query_unresolved_deliveries(limit=3)

    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


async def test_recovery_scan_rejects_nonadvancing_candidate_window() -> None:
    """A malformed DB window cannot spin the internal keyset loop forever."""
    candidate_limit = _candidate_batch_limit(1)
    storage = _RecoveryReadStub(
        [
            [
                {
                    "candidate_count": candidate_limit,
                    "scan_end_sequence": 0,
                    "receipt_id": None,
                }
            ]
        ]
    )

    with pytest.raises(RuntimeError, match="did not advance"):
        await storage.query_unresolved_deliveries(limit=1)


@pytest.mark.parametrize(
    "limit",
    [True, 0, MAX_RECOVERY_PAGE_LIMIT + 1],
)
def test_recovery_page_limit_rejects_invalid_values(limit: object) -> None:
    """Recovery paging rejects booleans and out-of-range page sizes."""
    with pytest.raises(ValueError):
        _validate_page_limit(limit)  # type: ignore[arg-type]
