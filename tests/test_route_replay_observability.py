"""Tests for route/replay metrics observability (Track 2 – diagnostics hardening).

Covers:
- Route counters are present in diagnostics after replay.
- Replay summary includes per-route breakdown.
- Sanitised errors remain clean after record_failed.
- Deterministic snapshot ordering across repeated calls.
- All tests use fake adapters / no live transports.
"""

from __future__ import annotations

import json

from medre.core.diagnostics.replay_metrics import ReplayMetrics
from medre.core.routing.stats import RouteStats
from medre.runtime.observability import DiagnosticsCollector

# ---------------------------------------------------------------------------
# test_route_counters_after_replay
# ---------------------------------------------------------------------------


def test_route_counters_after_replay() -> None:
    """After replay, diagnostics include route-aware counters."""
    collector = DiagnosticsCollector()

    # Simulate replay delivering through two routes
    collector.record_replay_events_processed("bridge-alpha")
    collector.record_replay_delivery_attempted("bridge-alpha")
    collector.record_replay_delivery_succeeded("bridge-alpha")

    collector.record_replay_events_processed("bridge-beta")
    collector.record_replay_delivery_attempted("bridge-beta")
    collector.record_replay_delivery_failed("bridge-beta")

    snap = collector.snapshot()

    # Global replay counters
    g = snap["replay"]["global"]
    assert g["replay_events_processed"] == 2
    assert g["replay_deliveries_attempted"] == 2
    assert g["replay_deliveries_succeeded"] == 1
    assert g["replay_deliveries_failed"] == 1

    # Per-route breakdown
    alpha = snap["replay"]["by_route"]["bridge-alpha"]
    assert alpha["events_processed"] == 1
    assert alpha["deliveries_succeeded"] == 1
    assert alpha["deliveries_failed"] == 0

    beta = snap["replay"]["by_route"]["bridge-beta"]
    assert beta["events_processed"] == 1
    assert beta["deliveries_succeeded"] == 0
    assert beta["deliveries_failed"] == 1


# ---------------------------------------------------------------------------
# test_replay_summary_includes_route_breakdown
# ---------------------------------------------------------------------------


def test_replay_summary_includes_route_breakdown() -> None:
    """Replay summary includes per-route counts with full attribution."""
    collector = DiagnosticsCollector()

    # Mix of filter skips, loop skips, and successful delivery
    collector.record_replay_events_processed("route-x")
    collector.record_replay_events_processed("route-x")
    collector.record_replay_delivery_attempted("route-x")
    collector.record_replay_delivery_succeeded("route-x")
    collector.record_replay_skipped_by_filter("route-x")

    collector.record_replay_events_processed("route-y")
    collector.record_replay_skipped_by_loop("route-y")

    snap = collector.snapshot()
    by_route = snap["replay"]["by_route"]

    # route-x breakdown
    rx = by_route["route-x"]
    assert rx["events_processed"] == 2
    assert rx["deliveries_attempted"] == 1
    assert rx["deliveries_succeeded"] == 1
    assert rx["skipped_by_filter"] == 1

    # route-y breakdown
    ry = by_route["route-y"]
    assert ry["events_processed"] == 1
    assert ry["skipped_by_loop"] == 1
    assert ry["deliveries_attempted"] == 0

    # Global totals
    g = snap["replay"]["global"]
    assert g["replay_events_processed"] == 3
    assert g["replay_skipped_by_filter"] == 1
    assert g["replay_skipped_by_loop"] == 1


# ---------------------------------------------------------------------------
# test_sanitized_errors_remain_sanitized
# ---------------------------------------------------------------------------


def test_sanitized_errors_remain_sanitized() -> None:
    """RouteStats.record_failed() keeps errors clean – no tokens or keys."""
    collector = DiagnosticsCollector()

    # Simulate a failure with embedded secrets and SDK reprs
    dirty_error = (
        "delivery failed: token=syt_abc123def456 "
        "api_key=sk-0123456789abcdef0123456789abcdef "
        "password=hunter2 "
        "secret=s3cret! "
        "<somed.Module object at 0x7f1234567890>"
    )

    collector.record_route_failed("bridge-zeta", dirty_error)
    snap = collector.snapshot()

    last_error = snap["routes"]["bridge-zeta"]["last_error"]
    assert "syt_" not in last_error
    assert "sk-0123" not in last_error
    assert "hunter2" not in last_error
    assert "s3cret" not in last_error
    assert "0x7f" not in last_error
    assert "[REDACTED]" in last_error
    assert "[OBJECT_REPR]" in last_error


def test_sanitized_errors_truncates_long_messages() -> None:
    """Errors longer than 512 characters are truncated."""
    collector = DiagnosticsCollector()
    long_error = "x" * 600
    collector.record_route_failed("bridge-long", long_error)

    snap = collector.snapshot()
    last_error = snap["routes"]["bridge-long"]["last_error"]
    assert len(last_error) <= 512


# ---------------------------------------------------------------------------
# test_deterministic_snapshot_ordering
# ---------------------------------------------------------------------------


def test_deterministic_snapshot_ordering() -> None:
    """Repeated snapshots have stable ordering of route and replay keys."""
    collector = DiagnosticsCollector()

    # Record events in non-alphabetical order
    for route_id in ["zeta-route", "alpha-route", "middle-route"]:
        collector.record_route_delivered(route_id)
        collector.record_replay_events_processed(route_id)
        collector.record_replay_delivery_succeeded(route_id)

    snap1 = collector.snapshot()
    snap2 = collector.snapshot()

    # Route keys must be sorted
    route_keys1 = list(snap1["routes"].keys())
    route_keys2 = list(snap2["routes"].keys())
    assert route_keys1 == route_keys2
    assert route_keys1 == sorted(route_keys1)

    # Replay by_route keys must be sorted
    replay_keys1 = list(snap1["replay"]["by_route"].keys())
    replay_keys2 = list(snap2["replay"]["by_route"].keys())
    assert replay_keys1 == replay_keys2
    assert replay_keys1 == sorted(replay_keys1)

    # Full snapshot must be JSON-serialisable with sort_keys
    json1 = json.dumps(snap1, sort_keys=True)
    json2 = json.dumps(snap2, sort_keys=True)
    assert json1 == json2


# ---------------------------------------------------------------------------
# test_snapshot_is_json_safe
# ---------------------------------------------------------------------------


def test_snapshot_is_json_safe() -> None:
    """Snapshot output is JSON-serialisable with no special types."""
    collector = DiagnosticsCollector()
    collector.record_route_delivered("r1")
    collector.record_route_failed("r2", "some error")
    collector.record_route_loop_prevented("r3")
    collector.record_replay_events_processed("r1")
    collector.record_replay_delivery_succeeded("r1")
    collector.record_replay_skipped_by_filter("r2")
    collector.record_replay_skipped_by_loop("r3")

    snap = collector.snapshot()
    # Must not raise
    serialized = json.dumps(snap, sort_keys=True)
    assert isinstance(serialized, str)


# ---------------------------------------------------------------------------
# test_empty_snapshot
# ---------------------------------------------------------------------------


def test_empty_snapshot() -> None:
    """Fresh collector produces empty but well-structured snapshot."""
    collector = DiagnosticsCollector()
    snap = collector.snapshot()

    assert snap["routes"] == {}
    assert snap["replay"]["global"]["replay_events_processed"] == 0
    assert snap["replay"]["by_route"] == {}


# ---------------------------------------------------------------------------
# test_route_stats_loop_prevented_counter
# ---------------------------------------------------------------------------


def test_route_stats_loop_prevented_counter() -> None:
    """RouteStats loop_prevented counter is accessible via diagnostics."""
    collector = DiagnosticsCollector()
    collector.record_route_loop_prevented("loop-route")
    collector.record_route_loop_prevented("loop-route")
    collector.record_route_delivered("loop-route")

    snap = collector.snapshot()
    lr = snap["routes"]["loop-route"]
    assert lr["delivered"] == 1
    assert lr["loop_prevented"] == 2


# ---------------------------------------------------------------------------
# test_replay_metrics_standalone
# ---------------------------------------------------------------------------


def test_replay_metrics_standalone() -> None:
    """ReplayMetrics can be used independently of DiagnosticsCollector."""
    rm = ReplayMetrics()
    rm.record_events_processed("standalone-route")
    rm.record_delivery_attempted("standalone-route")
    rm.record_delivery_succeeded("standalone-route")
    rm.record_skipped_by_loop("standalone-route")

    snap = rm.snapshot()
    assert snap["global"]["replay_events_processed"] == 1
    assert snap["global"]["replay_deliveries_succeeded"] == 1
    assert snap["global"]["replay_skipped_by_loop"] == 1

    sr = snap["by_route"]["standalone-route"]
    assert sr["deliveries_succeeded"] == 1
    assert sr["skipped_by_loop"] == 1


# ---------------------------------------------------------------------------
# test_route_stats_standalone_sanitization
# ---------------------------------------------------------------------------


def test_route_stats_standalone_sanitization() -> None:
    """RouteStats sanitizes errors when used standalone."""
    rs = RouteStats()
    rs.record_failed("r1", "error with sk-123456789012345678901234 inside")
    snap = rs.snapshot()
    assert "sk-1234" not in snap["r1"]["last_error"]
    assert "[REDACTED]" in snap["r1"]["last_error"]


# ---------------------------------------------------------------------------
# test_combined_route_and_replay_counters
# ---------------------------------------------------------------------------


def test_combined_route_and_replay_counters() -> None:
    """Route delivery and replay counters coexist in one snapshot."""
    collector = DiagnosticsCollector()

    # Normal route delivery
    collector.record_route_delivered("bridge-1")
    collector.record_route_delivered("bridge-1")
    collector.record_route_skipped("bridge-1")

    # Replay delivery
    collector.record_replay_events_processed("bridge-1")
    collector.record_replay_delivery_succeeded("bridge-1")

    snap = collector.snapshot()

    # Route counters unaffected by replay
    assert snap["routes"]["bridge-1"]["delivered"] == 2
    assert snap["routes"]["bridge-1"]["skipped"] == 1

    # Replay counters independent
    assert snap["replay"]["by_route"]["bridge-1"]["events_processed"] == 1
    assert snap["replay"]["by_route"]["bridge-1"]["deliveries_succeeded"] == 1
