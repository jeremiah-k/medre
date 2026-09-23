"""Structural contracts for bounded unresolved-delivery recovery queries."""

from __future__ import annotations

import re

import pytest

from medre.core.storage.backend import MAX_RECOVERY_PAGE_LIMIT
from medre.core.storage.sqlite._recovery_query import (
    _SELECT_UNRESOLVED_DELIVERIES,
    _SELECT_UNRESOLVED_DELIVERIES_SINCE,
    _validate_page_limit,
)
from medre.core.storage.sqlite.schema import _INDEXES


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
    assert "INDEXED BY idx_receipts_lineage" in sql
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


@pytest.mark.parametrize(
    "limit",
    [True, 0, MAX_RECOVERY_PAGE_LIMIT + 1],
)
def test_recovery_page_limit_rejects_invalid_values(limit: object) -> None:
    """Recovery paging rejects booleans and out-of-range page sizes."""
    with pytest.raises(ValueError):
        _validate_page_limit(limit)  # type: ignore[arg-type]
