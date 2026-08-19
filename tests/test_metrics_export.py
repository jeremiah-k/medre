"""Prometheus diagnostics export tests."""

from __future__ import annotations

import pytest

from medre.core.observability.export import snapshot_to_prometheus


def test_prometheus_export_emits_only_numeric_boolean_leaves() -> None:
    text = snapshot_to_prometheus(
        {
            "capacity": {"current": 2, "accepting": True, "ratio": 0.5},
            "runtime_state": "running",
            "error": "must-not-leak",
            "items": [1, 2, 3],
        }
    )

    assert "# TYPE medre_capacity_accepting gauge" in text
    assert "medre_capacity_accepting 1" in text
    assert "medre_capacity_current 2" in text
    assert "medre_capacity_ratio 0.5" in text
    assert "running" not in text
    assert "must-not-leak" not in text
    assert "items" not in text


def test_prometheus_export_is_deterministic_and_sanitizes_metric_names() -> None:
    snapshot = {"Adapter A": {"sent-count": 3}, "z": 1}
    first = snapshot_to_prometheus(snapshot)
    second = snapshot_to_prometheus(snapshot)

    assert first == second
    assert "medre_adapter_a_sent_count 3" in first
    assert first.endswith("\n")


def test_prometheus_export_rejects_sanitized_name_collisions() -> None:
    with pytest.raises(ValueError, match="metric name collision"):
        snapshot_to_prometheus({"a-b": 1, "a_b": 2})
