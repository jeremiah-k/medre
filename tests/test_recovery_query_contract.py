"""Structural contracts for bounded unresolved-delivery recovery queries."""

from __future__ import annotations

from medre.core.storage.sqlite._recovery_query import (
    _NO_LATER_RECEIPT,
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


def test_recovery_scan_uses_latest_receipt_existence_check() -> None:
    """Paging filters candidates instead of re-aggregating all lineages."""
    sql = _SELECT_UNRESOLVED_DELIVERIES

    assert "NOT EXISTS" in sql
    assert "newer.event_id = dr.event_id" in _NO_LATER_RECEIPT
    assert "newer.delivery_plan_id = dr.delivery_plan_id" in _NO_LATER_RECEIPT
    assert "newer.target_adapter = dr.target_adapter" in _NO_LATER_RECEIPT
    assert "COALESCE(newer.target_channel, '')" in _NO_LATER_RECEIPT
    assert "newer.sequence > dr.sequence" in _NO_LATER_RECEIPT
    assert "GROUP BY" not in sql
    assert "replay_run_id" not in _NO_LATER_RECEIPT


def test_since_scope_filters_the_same_event_lineage() -> None:
    """Canonical event time scopes candidates without changing lineage identity."""
    sql = _SELECT_UNRESOLVED_DELIVERIES_SINCE

    assert "JOIN canonical_events ce ON ce.event_id = dr.event_id" in sql
    assert "ce.timestamp >= ?" in sql
    assert "NOT EXISTS" in sql
    assert "replay_run_id" not in _NO_LATER_RECEIPT
