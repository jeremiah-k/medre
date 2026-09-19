"""Structural contracts for bounded unresolved-delivery recovery queries."""

from __future__ import annotations

from medre.core.storage.sqlite import _recovery_query
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


def test_since_scope_is_applied_before_lineage_aggregation() -> None:
    """Event-time scope limits lineage work without changing lineage identity."""
    sql = _recovery_query._SELECT_UNRESOLVED_DELIVERIES_SINCE

    assert "JOIN canonical_events ce_scope" in sql
    assert "WHERE ce_scope.timestamp >= ?" in sql
    assert sql.index("WHERE ce_scope.timestamp >= ?") < sql.index("GROUP BY dr.event_id")
    assert "replay_run_id" not in _recovery_query._LINEAGE_GROUP_BY
