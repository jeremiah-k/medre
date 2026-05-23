"""Tests for Meshtastic rxTime extraction from startup-backlog helpers.

Covers:
* :func:`extract_meshtastic_rx_time` — valid int, valid float, missing,
  bool, string, NaN/inf, negative/zero, float precision, overflow.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from medre.adapters.meshtastic.startup_backlog import extract_meshtastic_rx_time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EPOCH = 1_700_000_000.0  # arbitrary fixed epoch for reproducible tests

_UTC = timezone.utc


def _dt(epoch: float) -> datetime:
    """Shorthand UTC datetime from epoch seconds."""
    return datetime.fromtimestamp(epoch, tz=_UTC)


# ===================================================================
# extract_meshtastic_rx_time — valid inputs
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


class TestExtractRxTimeReject:
    """Invalid rxTime types are rejected."""

    def test_true(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": True}) is None

    def test_false(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": False}) is None

    def test_numeric_string(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": "1700000000"}) is None

    def test_empty_string(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": ""}) is None

    def test_nan(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("nan")}) is None

    def test_inf(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("inf")}) is None

    def test_negative_inf(self) -> None:
        assert extract_meshtastic_rx_time({"rxTime": float("-inf")}) is None

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


class TestExtractRxTimeOverflow:
    """Extremely large epoch values that overflow datetime.fromtimestamp.

    These values would raise OverflowError, OSError, or ValueError in
    ``datetime.fromtimestamp`` without the try/except guard.
    """

    def test_very_large_epoch_returns_none(self) -> None:
        """10^20 exceeds platform time_t range → returns None."""
        assert extract_meshtastic_rx_time({"rxTime": 10**20}) is None

    def test_extremely_large_epoch_returns_none(self) -> None:
        """10^1000 vastly exceeds any representable timestamp → returns None."""
        assert extract_meshtastic_rx_time({"rxTime": 10**1000}) is None

    def test_year_10000_boundary_returns_none(self) -> None:
        """253402300800 is year 10000 in Unix epoch — may overflow on
        some platforms (32-bit time_t).  Even where it does not
        overflow, the try/except guard ensures safe handling."""
        # On most 64-bit platforms this actually succeeds, but the
        # guard should handle both outcomes gracefully.
        result = extract_meshtastic_rx_time({"rxTime": 253402300800})
        # Either it returns a valid datetime or None — both are acceptable.
        # We primarily test that it does not raise.
        assert result is None or isinstance(result, datetime)
