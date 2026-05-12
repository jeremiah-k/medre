"""Tests for runtime boot summary.

Covers:
- BootSummary construction and immutability.
- Deterministic to_dict output (sorted keys).
- JSON serialisability.
- build_boot_summary sorts and converts adapter ID lists.
- All fields are plain types (no SDK objects).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.runtime.boot_summary import BootSummary, build_boot_summary


class TestBootSummaryConstruction:
    """BootSummary is a frozen dataclass with correct defaults."""

    def test_construction(self) -> None:
        """Basic construction succeeds."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=2,
            adapters_failed=0,
            adapters_total=2,
            adapters_disabled=1,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a1", "a2"],
            route_count=3,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=42,
        )
        assert bs.startup_outcome == "success"
        assert bs.runtime_health == "healthy"
        assert bs.adapters_started == 2
        assert bs.route_count == 3
        assert bs.persisted_events_count == 42

    def test_frozen(self) -> None:
        """BootSummary is immutable."""
        bs = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        with pytest.raises(AttributeError):
            bs.startup_outcome = "partial"  # type: ignore[misc]

    def test_adapter_ids_sorted_as_tuples(self) -> None:
        """Adapter ID lists are sorted and stored as tuples."""
        bs = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=2,
            adapters_failed=1,
            adapters_total=3,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=["z-fail", "a-fail"],
            started_adapter_ids=["z-ok", "a-ok"],
            route_count=0,
            storage_backend="sqlite",
            replay_available=False,
            persisted_events_count=None,
        )
        assert bs.failed_adapter_ids == ("a-fail", "z-fail")
        assert bs.started_adapter_ids == ("a-ok", "z-ok")
        assert isinstance(bs.failed_adapter_ids, tuple)
        assert isinstance(bs.started_adapter_ids, tuple)


class TestBootSummaryToDict:
    """to_dict produces deterministic, JSON-safe output."""

    def test_keys_sorted(self) -> None:
        """to_dict keys are alphabetically sorted."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a1"],
            route_count=0,
            storage_backend="sqlite",
            replay_available=False,
            persisted_events_count=0,
        )
        d = bs.to_dict()
        assert list(d.keys()) == sorted(d.keys())

    def test_json_serialisable(self) -> None:
        """Full to_dict output is JSON-serialisable."""
        bs = build_boot_summary(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="partial",
            runtime_health="degraded",
            adapters_started=1,
            adapters_failed=1,
            adapters_total=2,
            adapters_disabled=1,
            build_failure_count=1,
            failed_adapter_ids=["bad"],
            started_adapter_ids=["ok"],
            route_count=5,
            storage_backend="sqlite",
            replay_available=True,
            persisted_events_count=100,
        )
        serialized = json.dumps(bs.to_dict(), sort_keys=True)
        assert isinstance(serialized, str)

    def test_deterministic(self) -> None:
        """Same inputs produce identical JSON."""
        kwargs = dict(
            startup_timestamp="2026-05-11T12:00:00+00:00",
            startup_outcome="success",
            runtime_health="healthy",
            adapters_started=1,
            adapters_failed=0,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=[],
            started_adapter_ids=["a"],
            route_count=0,
            storage_backend="memory",
            replay_available=False,
            persisted_events_count=None,
        )
        bs1 = build_boot_summary(**kwargs)
        bs2 = build_boot_summary(**kwargs)
        assert json.dumps(bs1.to_dict(), sort_keys=True) == json.dumps(bs2.to_dict(), sort_keys=True)

    def test_none_fields_serialise(self) -> None:
        """None fields serialise as null in JSON."""
        bs = build_boot_summary(
            startup_timestamp=None,
            startup_outcome="total_failure",
            runtime_health="failed",
            adapters_started=0,
            adapters_failed=1,
            adapters_total=1,
            adapters_disabled=0,
            build_failure_count=0,
            failed_adapter_ids=["x"],
            started_adapter_ids=[],
            route_count=0,
            storage_backend="none",
            replay_available=False,
            persisted_events_count=None,
        )
        d = bs.to_dict()
        assert d["startup_timestamp"] is None
        assert d["persisted_events_count"] is None
        # JSON round-trip
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["startup_timestamp"] is None
