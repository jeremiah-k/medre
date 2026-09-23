"""Structural contracts for bounded unresolved-delivery recovery queries."""

from __future__ import annotations

from medre.core.storage.sqlite._recovery_query import (
    _SELECT_UNRESOLVED_DELIVERIES,
    _SELECT_UNRESOLVED_DELIVERIES_SINCE,
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


def test_recovery_scan_consumes_authority_projection() -> None:
    """The scan pages over the delivery_status authority projection.

    Current-receipt ordering cannot drift into a second correlated-SQL
    definition here: the view already applies the generation-aware rule.
    """
    sql = _SELECT_UNRESOLVED_DELIVERIES

    assert "FROM delivery_status dr" in sql
    assert "NOT EXISTS" not in sql
    assert "GROUP BY" not in sql
    assert "OFFSET" not in sql
    # replay_run_id is a selected provenance column, never a lineage
    # predicate.
    where_clause = sql.split(" WHERE ", 1)[1] if " WHERE " in sql else ""
    assert "replay_run_id" not in where_clause


def test_since_scope_filters_the_same_event_lineage() -> None:
    """Canonical event time scopes candidates without changing lineage identity."""
    sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE

    assert "JOIN canonical_events ce ON ce.event_id = dr.event_id" in sql
    assert "ce.timestamp >= ?" in sql
    assert "FROM delivery_status dr" in sql
    where_clause = sql.split(" WHERE ", 1)[1] if " WHERE " in sql else ""
    assert "replay_run_id" not in where_clause
