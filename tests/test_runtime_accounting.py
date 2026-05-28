"""Tests for runtime event accounting counters (Track 5 – runtime accounting).

Covers:
- Fresh instance has all counters at zero.
- Each record_* method increments exactly the target counter.
- Multiple increments accumulate correctly.
- Independent counters: incrementing one does not affect others.
- reset() returns previous values and zeros all counters.
- reset() is idempotent on already-zero instance.
- snapshot() keys are alphabetically sorted.
- snapshot() output is JSON-serialisable.
- Memory is bounded: snapshot size is constant.
- RuntimeCounters is frozen (immutable).
- counters() returns RuntimeCounters instance.
- to_dict() is an alias for snapshot().
- Repr is informative.
"""

from __future__ import annotations

import json
import sys

import pytest

from medre.core.supervision.accounting import RuntimeAccounting, RuntimeCounters

# ---------------------------------------------------------------------------
# Fresh instance
# ---------------------------------------------------------------------------


class TestFreshInstance:
    """Fresh RuntimeAccounting has all counters at zero."""

    def test_all_counters_zero(self) -> None:
        acc = RuntimeAccounting()
        c = acc.counters()
        assert c.inbound_accepted == 0
        assert c.outbound_attempts == 0
        assert c.outbound_delivered == 0
        assert c.outbound_failed == 0
        assert c.replay_processed == 0
        assert c.replay_rejected == 0
        assert c.loop_prevented == 0
        assert c.capacity_rejections == 0
        assert c.policy_suppressed == 0

    def test_snapshot_all_zero(self) -> None:
        snap = RuntimeAccounting().snapshot()
        assert all(v == 0 for v in snap.values())

    def test_ten_counters_exactly(self) -> None:
        """Exactly 10 counters exist — no more, no fewer."""
        RuntimeCounters()
        assert len(RuntimeCounters.__dataclass_fields__) == 10
        snap = RuntimeAccounting().snapshot()
        assert len(snap) == 10


# ---------------------------------------------------------------------------
# Individual increment tests
# ---------------------------------------------------------------------------


class TestIndividualIncrements:
    """Each record_* method increments exactly the target counter."""

    def test_record_inbound_accepted(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        c = acc.counters()
        assert c.inbound_accepted == 1
        # All others remain zero
        assert c.outbound_attempts == 0
        assert c.outbound_delivered == 0
        assert c.outbound_failed == 0
        assert c.replay_processed == 0
        assert c.replay_rejected == 0
        assert c.loop_prevented == 0
        assert c.capacity_rejections == 0
        assert c.policy_suppressed == 0

    def test_record_outbound_attempt(self) -> None:
        acc = RuntimeAccounting()
        acc.record_outbound_attempt()
        assert acc.counters().outbound_attempts == 1
        assert acc.counters().inbound_accepted == 0

    def test_record_outbound_delivered(self) -> None:
        acc = RuntimeAccounting()
        acc.record_outbound_delivered()
        assert acc.counters().outbound_delivered == 1
        assert acc.counters().outbound_failed == 0

    def test_record_outbound_failed(self) -> None:
        acc = RuntimeAccounting()
        acc.record_outbound_failed()
        assert acc.counters().outbound_failed == 1
        assert acc.counters().outbound_delivered == 0

    def test_record_replay_processed(self) -> None:
        acc = RuntimeAccounting()
        acc.record_replay_processed()
        assert acc.counters().replay_processed == 1
        assert acc.counters().replay_rejected == 0

    def test_record_replay_rejected(self) -> None:
        acc = RuntimeAccounting()
        acc.record_replay_rejected()
        assert acc.counters().replay_rejected == 1
        assert acc.counters().replay_processed == 0

    def test_record_loop_prevented(self) -> None:
        acc = RuntimeAccounting()
        acc.record_loop_prevented()
        assert acc.counters().loop_prevented == 1
        assert acc.counters().capacity_rejections == 0

    def test_record_capacity_rejection(self) -> None:
        acc = RuntimeAccounting()
        acc.record_capacity_rejection()
        assert acc.counters().capacity_rejections == 1
        assert acc.counters().loop_prevented == 0

    def test_record_policy_suppressed(self) -> None:
        acc = RuntimeAccounting()
        acc.record_policy_suppressed()
        c = acc.counters()
        assert c.policy_suppressed == 1
        assert c.capacity_rejections == 0
        assert c.outbound_failed == 0

    def test_record_capability_suppressed(self) -> None:
        acc = RuntimeAccounting()
        acc.record_capability_suppressed()
        assert acc.snapshot()["capability_suppressed"] == 1


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


class TestAccumulation:
    """Multiple increments accumulate correctly."""

    def test_repeated_inbound(self) -> None:
        acc = RuntimeAccounting()
        for _ in range(50):
            acc.record_inbound_accepted()
        assert acc.counters().inbound_accepted == 50

    def test_mixed_increments(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        acc.record_outbound_delivered()
        acc.record_outbound_failed()
        acc.record_loop_prevented()
        acc.record_capacity_rejection()
        acc.record_capacity_rejection()
        acc.record_replay_processed()
        acc.record_replay_rejected()
        acc.record_policy_suppressed()

        c = acc.counters()
        assert c.inbound_accepted == 2
        assert c.outbound_attempts == 1
        assert c.outbound_delivered == 1
        assert c.outbound_failed == 1
        assert c.loop_prevented == 1
        assert c.capacity_rejections == 2
        assert c.replay_processed == 1
        assert c.replay_rejected == 1
        assert c.policy_suppressed == 1


# ---------------------------------------------------------------------------
# Independent counters
# ---------------------------------------------------------------------------


class TestIndependence:
    """Incrementing one counter does not affect others."""

    def test_inbound_does_not_affect_outbound(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        c = acc.counters()
        assert c.inbound_accepted == 3
        assert c.outbound_attempts == 0
        assert c.outbound_delivered == 0
        assert c.outbound_failed == 0

    def test_outbound_does_not_affect_replay(self) -> None:
        acc = RuntimeAccounting()
        acc.record_outbound_attempt()
        acc.record_outbound_delivered()
        acc.record_outbound_failed()
        c = acc.counters()
        assert c.replay_processed == 0
        assert c.replay_rejected == 0


# ---------------------------------------------------------------------------
# Reset semantics
# ---------------------------------------------------------------------------


class TestReset:
    """reset() returns previous values and zeros all counters."""

    def test_reset_returns_previous(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_delivered()
        previous = acc.reset()
        assert previous.inbound_accepted == 1
        assert previous.outbound_delivered == 1

    def test_reset_zeros_counters(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_capacity_rejection()
        acc.reset()
        c = acc.counters()
        assert c.inbound_accepted == 0
        assert c.capacity_rejections == 0

    def test_reset_idempotent(self) -> None:
        """Resetting an already-zero instance returns all-zeros."""
        acc = RuntimeAccounting()
        previous = acc.reset()
        assert previous.inbound_accepted == 0
        # Second reset
        previous2 = acc.reset()
        assert previous2.inbound_accepted == 0

    def test_reset_then_increment(self) -> None:
        """Counters work correctly after reset."""
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.reset()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        assert acc.counters().inbound_accepted == 2


# ---------------------------------------------------------------------------
# New instance isolation
# ---------------------------------------------------------------------------


class TestInstanceIsolation:
    """Different instances do not share state."""

    def test_independent_instances(self) -> None:
        a = RuntimeAccounting()
        b = RuntimeAccounting()
        a.record_inbound_accepted()
        a.record_inbound_accepted()
        b.record_loop_prevented()
        assert a.counters().inbound_accepted == 2
        assert a.counters().loop_prevented == 0
        assert b.counters().inbound_accepted == 0
        assert b.counters().loop_prevented == 1


# ---------------------------------------------------------------------------
# Deterministic snapshot ordering
# ---------------------------------------------------------------------------


class TestSnapshotOrdering:
    """snapshot() keys are alphabetically sorted and deterministic."""

    def test_keys_alphabetically_sorted(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_delivered()
        snap = acc.snapshot()
        keys = list(snap.keys())
        assert keys == sorted(keys)

    def test_snapshot_deterministic_across_calls(self) -> None:
        """Two snapshot calls with no mutations return identical output."""
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        snap1 = acc.snapshot()
        snap2 = acc.snapshot()
        assert snap1 == snap2
        assert list(snap1.keys()) == list(snap2.keys())

    def test_snapshot_deterministic_after_mixed_operations(self) -> None:
        """Snapshot ordering is stable regardless of which counters were
        incremented first."""
        acc = RuntimeAccounting()
        # Increment in reverse alphabetical order of counter names
        acc.record_replay_rejected()
        acc.record_replay_processed()
        acc.record_policy_suppressed()
        acc.record_outbound_failed()
        acc.record_outbound_delivered()
        acc.record_outbound_attempt()
        acc.record_loop_prevented()
        acc.record_inbound_accepted()
        acc.record_capacity_rejection()

        snap = acc.snapshot()
        keys = list(snap.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# JSON safety
# ---------------------------------------------------------------------------


class TestJsonSafety:
    """snapshot() output is JSON-serialisable."""

    def test_json_dumps_succeeds(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_delivered()
        acc.record_capacity_rejection()
        text = json.dumps(acc.snapshot())
        assert isinstance(text, str)

    def test_json_roundtrip_preserves_values(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_attempt()
        acc.record_outbound_delivered()
        acc.record_outbound_failed()
        snap = acc.snapshot()
        roundtripped = json.loads(json.dumps(snap))
        assert roundtripped == snap

    def test_all_values_are_int(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        for v in acc.snapshot().values():
            assert isinstance(v, int)

    def test_no_secrets_in_snapshot(self) -> None:
        """Snapshot values are plain ints — no tokens, no objects."""
        acc = RuntimeAccounting()
        snap = acc.snapshot()
        text = json.dumps(snap)
        # Should not contain any suspicious patterns
        assert "token" not in text.lower()
        assert "secret" not in text.lower()
        assert "password" not in text.lower()


# ---------------------------------------------------------------------------
# Bounded memory
# ---------------------------------------------------------------------------


class TestBoundedMemory:
    """Snapshot size is constant regardless of counter values."""

    def test_snapshot_size_constant(self) -> None:
        acc = RuntimeAccounting()
        snap_empty = acc.snapshot()
        # Increment counters many times
        for _ in range(1000):
            acc.record_inbound_accepted()
            acc.record_outbound_attempt()
            acc.record_outbound_delivered()
            acc.record_outbound_failed()
            acc.record_replay_processed()
            acc.record_replay_rejected()
            acc.record_loop_prevented()
            acc.record_capacity_rejection()
        snap_full = acc.snapshot()
        # Same number of keys
        assert len(snap_empty) == len(snap_full) == 10
        # Same key set
        assert set(snap_empty.keys()) == set(snap_full.keys())

    def test_counters_object_size_constant(self) -> None:
        """RuntimeCounters size does not grow with counter values."""
        acc = RuntimeAccounting()
        c1 = acc.counters()
        for _ in range(100):
            acc.record_inbound_accepted()
        c2 = acc.counters()
        assert sys.getsizeof(c1) == sys.getsizeof(c2)


# ---------------------------------------------------------------------------
# RuntimeCounters frozen (immutable)
# ---------------------------------------------------------------------------


class TestRuntimeCountersFrozen:
    """RuntimeCounters is frozen — attribute assignment raises."""

    def test_frozen_attribute_error(self) -> None:
        c = RuntimeCounters()
        with pytest.raises(AttributeError):
            c.inbound_accepted = 5  # type: ignore[misc]

    def test_frozen_no_new_attributes(self) -> None:
        c = RuntimeCounters()
        with pytest.raises(AttributeError):
            c.new_field = 42  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# counters() return type
# ---------------------------------------------------------------------------


class TestCountersMethod:
    """counters() returns a RuntimeCounters instance."""

    def test_returns_runtime_counters(self) -> None:
        acc = RuntimeAccounting()
        c = acc.counters()
        assert isinstance(c, RuntimeCounters)

    def test_counters_reflects_state(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_inbound_accepted()
        c = acc.counters()
        assert c.inbound_accepted == 2


# ---------------------------------------------------------------------------
# to_dict alias
# ---------------------------------------------------------------------------


class TestToDict:
    """to_dict() returns the same result as snapshot()."""

    def test_to_dict_equals_snapshot(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        acc.record_outbound_delivered()
        assert acc.to_dict() == acc.snapshot()


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------


class TestRepr:
    """Repr is informative."""

    def test_repr_contains_counters(self) -> None:
        acc = RuntimeAccounting()
        r = repr(acc)
        assert "RuntimeAccounting" in r
        assert "RuntimeCounters" in r

    def test_repr_shows_nonzero_values(self) -> None:
        acc = RuntimeAccounting()
        acc.record_inbound_accepted()
        r = repr(acc)
        assert "inbound_accepted=1" in r


# ---------------------------------------------------------------------------
# Re-export from runtime package
# ---------------------------------------------------------------------------


class TestReExport:
    """RuntimeAccounting and RuntimeCounters are re-exported from
    medre.core.supervision."""

    def test_import_from_runtime_package(self) -> None:
        from medre.core.supervision import RuntimeAccounting as RA
        from medre.core.supervision import RuntimeCounters as RC

        assert RA is RuntimeAccounting
        assert RC is RuntimeCounters
