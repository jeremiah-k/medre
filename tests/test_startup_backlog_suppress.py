"""Tests for startup-backlog suppression utilities.

Covers:
* :func:`extract_meshtastic_rx_time` — valid int, valid float, missing,
  bool, string, NaN/inf, negative/zero, float precision.
* :func:`should_suppress_startup_backlog` — stale, fresh, within-window,
  exact cutoff, just-before-cutoff, disabled (zero / negative), missing
  packet_time, future timestamps, timezone normalization, naive datetimes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from medre.core.policies.startup_backlog_suppress import (
    extract_meshtastic_rx_time,
    should_suppress_startup_backlog,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EPOCH = 1_700_000_000.0  # arbitrary fixed epoch for reproducible tests

_UTC = timezone.utc


def _dt(epoch: float) -> datetime:
    """Shorthand UTC datetime from epoch seconds."""
    return datetime.fromtimestamp(epoch, tz=_UTC)


def _naive(epoch: float) -> datetime:
    """Shorthand naive datetime from epoch seconds."""
    return datetime.fromtimestamp(epoch, tz=_UTC).replace(tzinfo=None)


# ===================================================================
# extract_meshtastic_rx_time
# ===================================================================


class TestExtractRxTimeValid:
    """Valid rxTime values produce a timezone-aware UTC datetime."""

    def test_int_epoch(self) -> None:
        pkt = {"rxTime": 1_700_000_000}
        result = extract_meshtastic_rx_time(pkt)
        assert result is not None
        assert result == _dt(1_700_000_000)
        assert result.tzinfo is _UTC

    def test_float_epoch(self) -> None:
        pkt = {"rxTime": 1_700_000_000.123}
        result = extract_meshtastic_rx_time(pkt)
        assert result is not None
        assert abs(result.timestamp() - 1_700_000_000.123) < 1e-6

    def test_small_positive_int(self) -> None:
        pkt = {"rxTime": 1}
        result = extract_meshtastic_rx_time(pkt)
        assert result is not None
        assert result == _dt(1.0)

    def test_small_positive_float(self) -> None:
        pkt = {"rxTime": 0.001}
        result = extract_meshtastic_rx_time(pkt)
        assert result is not None
        assert result.timestamp() == pytest.approx(0.001)

    def test_returns_none_for_empty_mapping(self) -> None:
        result = extract_meshtastic_rx_time({})
        assert result is None


class TestExtractRxTimeMissing:
    """Missing rxTime returns None."""

    def test_no_rx_time_key(self) -> None:
        assert extract_meshtastic_rx_time({"fromId": "!abc"}) is None

    def test_rx_time_none_value(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": None}) is None


class TestExtractRxTimeBool:
    """Bool values (subclass of int) are rejected."""

    def test_true(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": True}) is None

    def test_false(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": False}) is None


class TestExtractRxTimeString:
    """String rxTime is rejected."""

    def test_numeric_string(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": "1700000000"}) is None

    def test_empty_string(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": ""}) is None


class TestExtractRxTimeNanInf:
    """NaN / inf / -inf float values are rejected."""

    def test_nan(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("nan")}) is None

    def test_inf(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("inf")}) is None

    def test_negative_inf(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("-inf")}) is None


class TestExtractRxTimeNonPositive:
    """Zero and negative epochs are rejected."""

    def test_zero_int(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": 0}) is None

    def test_zero_float(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": 0.0}) is None

    def test_negative_int(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": -1}) is None

    def test_negative_float(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": -0.5}) is None


class TestExtractRxTimeMisc:
    """Other edge cases for rxTime extraction."""

    def test_dict_value(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": {"seconds": 100}}) is None

    def test_list_value(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": [100]}) is None

    def test_extra_keys_ignored(self) -> None:
        pkt = {"rxTime": 1_700_000_000, "decoded": {"portnum": "text"}}
        result = extract_meshtastic_rx_time(pkt)
        assert result is not None
        assert result == _dt(1_700_000_000)


# ===================================================================
# should_suppress_startup_backlog
# ===================================================================

_START_TIME = _dt(_EPOCH)
_SUPPRESS_WINDOW = 30.0  # 30 seconds


class TestSuppressStale:
    """Packet well before cutoff is suppressed."""

    def test_stale_packet(self) -> None:
        pkt_time = _dt(_EPOCH - 60.0)  # 60 s before start
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )

    def test_just_before_cutoff(self) -> None:
        """Packet timestamp 0.001 s before cutoff → suppressed."""
        cutoff = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff - 0.001)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )


class TestSuppressFresh:
    """Packet after cutoff is not suppressed."""

    def test_fresh_packet_after_start(self) -> None:
        pkt_time = _dt(_EPOCH + 10.0)  # 10 s after start
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )

    def test_fresh_packet_at_start(self) -> None:
        """Packet timestamp == adapter start time → not suppressed."""
        assert (
            should_suppress_startup_backlog(_START_TIME, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )


class TestSuppressWithinWindow:
    """Packet inside the window but at or after cutoff is not suppressed."""

    def test_packet_at_cutoff(self) -> None:
        """Exact cutoff → NOT suppressed (equality allowed)."""
        cutoff = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )

    def test_packet_just_after_cutoff(self) -> None:
        """0.001 s after cutoff → not suppressed."""
        cutoff = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff + 0.001)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )


class TestSuppressDisabled:
    """Zero or negative suppress_seconds disables suppression."""

    def test_disabled_zero(self) -> None:
        pkt_time = _dt(_EPOCH - 1000.0)  # very stale
        assert should_suppress_startup_backlog(pkt_time, _START_TIME, 0.0) is False

    def test_disabled_negative(self) -> None:
        pkt_time = _dt(_EPOCH - 1000.0)
        assert should_suppress_startup_backlog(pkt_time, _START_TIME, -10.0) is False

    def test_disabled_still_returns_false_for_fresh(self) -> None:
        pkt_time = _dt(_EPOCH + 5.0)
        assert should_suppress_startup_backlog(pkt_time, _START_TIME, -1.0) is False


class TestSuppressMissingPacketTime:
    """None packet_time → not suppressed (no evidence of staleness)."""

    def test_none_packet_time(self) -> None:
        assert (
            should_suppress_startup_backlog(None, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )

    def test_none_packet_time_disabled(self) -> None:
        assert should_suppress_startup_backlog(None, _START_TIME, 0.0) is False


class TestSuppressFuture:
    """Future timestamps are never suppressed."""

    def test_future_packet(self) -> None:
        pkt_time = _dt(_EPOCH + 3600.0)  # 1 hour in the future
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )

    def test_future_packet_large_window(self) -> None:
        """Even with a huge window, future packets are not suppressed."""
        pkt_time = _dt(_EPOCH + 1.0)  # 1 s in the future
        assert should_suppress_startup_backlog(pkt_time, _START_TIME, 1e6) is False


class TestSuppressTimezoneNormalization:
    """Timezone-aware datetimes are normalized to UTC correctly."""

    def test_aware_utc(self) -> None:
        pkt_time = _dt(_EPOCH - 60.0)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )

    def test_aware_non_utc(self) -> None:
        """A non-UTC timezone is converted to UTC for comparison."""
        # UTC+2 is 2 hours ahead; the epoch is the same instant.
        offset_tz = timezone(timedelta(hours=2))
        # Same instant as _EPOCH - 60 in UTC → expressed in UTC+2.
        pkt_time = datetime.fromtimestamp(_EPOCH - 60.0, tz=offset_tz)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )

    def test_naive_packet_time_treated_as_utc(self) -> None:
        """Naive packet_time is conservatively assumed UTC."""
        pkt_time = _naive(_EPOCH - 60.0)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )

    def test_naive_start_time_treated_as_utc(self) -> None:
        """Naive adapter_start_time is conservatively assumed UTC."""
        start = _naive(_EPOCH)
        pkt_time = _dt(_EPOCH - 60.0)
        assert (
            should_suppress_startup_backlog(pkt_time, start, _SUPPRESS_WINDOW) is True
        )

    def test_both_naive(self) -> None:
        """Both naive → treated as UTC, comparison still works."""
        start = _naive(_EPOCH)
        pkt_time = _naive(_EPOCH - 60.0)
        assert (
            should_suppress_startup_backlog(pkt_time, start, _SUPPRESS_WINDOW) is True
        )


class TestSuppressBoundaryExactness:
    """Exhaustive boundary checks around the cutoff."""

    def test_exact_cutoff_not_suppressed(self) -> None:
        cutoff_epoch = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff_epoch)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )

    def test_one_microsecond_before_cutoff_suppressed(self) -> None:
        cutoff_epoch = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff_epoch - 1e-6)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is True
        )

    def test_one_microsecond_after_cutoff_not_suppressed(self) -> None:
        cutoff_epoch = _EPOCH - _SUPPRESS_WINDOW
        pkt_time = _dt(cutoff_epoch + 1e-6)
        assert (
            should_suppress_startup_backlog(pkt_time, _START_TIME, _SUPPRESS_WINDOW)
            is False
        )


class TestSuppressSmallWindow:
    """Edge cases with very small suppression windows."""

    def test_window_one_millisecond(self) -> None:
        pkt_stale = _dt(_EPOCH - 0.002)  # 2 ms before start
        assert should_suppress_startup_backlog(pkt_stale, _START_TIME, 0.001) is True

    def test_window_one_millisecond_fresh(self) -> None:
        pkt_fresh = _dt(_EPOCH - 0.0005)  # 0.5 ms before start, within 1 ms
        assert should_suppress_startup_backlog(pkt_fresh, _START_TIME, 0.001) is False
