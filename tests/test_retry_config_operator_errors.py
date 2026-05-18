"""Retry config operator error quality tests.

Validates that operators receive concise, actionable error messages for
common retry misconfiguration scenarios — without needing to read source
code.

Scenarios covered:

1. Invalid retry TOML type for max_attempts (string instead of int)
2. Invalid retry TOML type for interval_seconds (string instead of number)
3. Invalid retry TOML type for enabled (string instead of bool)
4. Negative max_attempts
5. Zero interval_seconds (must be > 0)
6. Negative batch_size
7. Route retry enabled while global worker disabled — snapshot reflects disabled
8. Missing adapter during retry — ADAPTER_MISSING classification
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import _validate_retry_section
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryExecutor,
    RetryPolicy,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 1. Invalid retry TOML type: max_attempts
# ---------------------------------------------------------------------------


class TestInvalidRetryMaxAttemptsType:
    """max_attempts must be an integer; string produces ConfigValidationError."""

    def test_max_attempts_string_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"max_attempts": "three"})
        msg = str(exc_info.value)
        assert "max_attempts" in msg
        assert "must be an integer" in msg
        assert "'three'" in msg
        assert "Traceback" not in msg

    def test_max_attempts_float_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"max_attempts": 3.5})
        msg = str(exc_info.value)
        assert "max_attempts" in msg
        assert "must be an integer" in msg

    def test_max_attempts_bool_raises(self) -> None:
        """Python bool is a subclass of int; we reject it explicitly."""
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"max_attempts": True})
        msg = str(exc_info.value)
        assert "max_attempts" in msg
        assert "must be an integer" in msg


# ---------------------------------------------------------------------------
# 2. Negative max_attempts
# ---------------------------------------------------------------------------


class TestNegativeMaxAttempts:
    """Negative max_attempts produces ConfigValidationError with range."""

    def test_negative_max_attempts(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"max_attempts": -1})
        msg = str(exc_info.value)
        assert "max_attempts" in msg
        assert "must be >= 1" in msg
        assert "-1" in msg

    def test_zero_max_attempts(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"max_attempts": 0})
        msg = str(exc_info.value)
        assert "max_attempts" in msg
        assert "must be >= 1" in msg


# ---------------------------------------------------------------------------
# 3. Invalid interval_seconds type
# ---------------------------------------------------------------------------


class TestInvalidIntervalType:
    """interval_seconds must be a number; string produces ConfigValidationError."""

    def test_interval_string_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"interval_seconds": "fast"})
        msg = str(exc_info.value)
        assert "interval_seconds" in msg
        assert "must be a number" in msg
        assert "'fast'" in msg

    def test_interval_bool_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"interval_seconds": False})
        msg = str(exc_info.value)
        assert "interval_seconds" in msg
        assert "must be a number" in msg

    def test_interval_zero_raises(self) -> None:
        """interval_seconds must be > 0."""
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"interval_seconds": 0})
        msg = str(exc_info.value)
        assert "interval_seconds" in msg
        assert "must be > 0" in msg

    def test_interval_negative_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"interval_seconds": -5.0})
        msg = str(exc_info.value)
        assert "interval_seconds" in msg
        assert "must be > 0" in msg


# ---------------------------------------------------------------------------
# 4. Invalid jitter type (enabled field as string)
# ---------------------------------------------------------------------------


class TestInvalidEnabledType:
    """enabled must be a boolean; string produces ConfigValidationError."""

    def test_enabled_string_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"enabled": "yes"})
        msg = str(exc_info.value)
        assert "enabled" in msg
        assert "must be a boolean" in msg
        assert "'yes'" in msg

    def test_enabled_int_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"enabled": 1})
        msg = str(exc_info.value)
        assert "enabled" in msg
        assert "must be a boolean" in msg


# ---------------------------------------------------------------------------
# 5. Invalid batch_size
# ---------------------------------------------------------------------------


class TestInvalidBatchSize:
    """batch_size must be a positive integer."""

    def test_batch_size_string_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"batch_size": "many"})
        msg = str(exc_info.value)
        assert "batch_size" in msg
        assert "must be an integer" in msg

    def test_batch_size_zero_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"batch_size": 0})
        msg = str(exc_info.value)
        assert "batch_size" in msg
        assert "must be >= 1" in msg

    def test_batch_size_negative_raises(self) -> None:
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_retry_section({"batch_size": -10})
        msg = str(exc_info.value)
        assert "batch_size" in msg
        assert "must be >= 1" in msg


# ---------------------------------------------------------------------------
# 6. Valid retry config passes validation
# ---------------------------------------------------------------------------


class TestValidRetryConfig:
    """Valid retry config values pass validation without error."""

    def test_defaults_pass(self) -> None:
        _validate_retry_section({})

    def test_valid_values_pass(self) -> None:
        _validate_retry_section({
            "enabled": True,
            "interval_seconds": 5.0,
            "batch_size": 10,
            "max_attempts": 3,
        })

    def test_valid_integer_interval_passes(self) -> None:
        """TOML integers are valid for interval_seconds."""
        _validate_retry_section({"interval_seconds": 10})


# ---------------------------------------------------------------------------
# 7. Route retry enabled while global worker disabled — snapshot state
# ---------------------------------------------------------------------------


class TestRetryDisabledSnapshotState:
    """When [retry] enabled=false (default), the snapshot retry section
    reflects disabled state — even if routes would create retry receipts.

    This tests the snapshot contract, not the core retry worker.
    """

    def test_snapshot_retry_disabled_by_default(self) -> None:
        """build_runtime_snapshot produces retry.enabled=false when no
        retry worker is configured."""
        from medre.runtime.snapshot import build_runtime_snapshot

        # Minimal app mock with no retry_state attribute
        app = MagicMock(spec=[])
        del app.retry_state  # ensure attribute doesn't exist

        snapshot = build_runtime_snapshot(app)
        assert snapshot["retry"]["enabled"] is False
        assert snapshot["retry"]["running"] is False
        assert snapshot["retry"]["processed"] == 0

    def test_snapshot_retry_enabled_reflects_state(self) -> None:
        """When retry_state is present and enabled, snapshot reflects it."""
        from medre.runtime.snapshot import build_runtime_snapshot
        from medre.runtime.retry import RetryWorkerState

        state = RetryWorkerState(
            enabled=True,
            running=True,
            last_run_at="2026-05-16T00:00:00Z",
            processed=5,
            succeeded=3,
            failed=1,
            dead_lettered=1,
        )

        app = MagicMock()
        app.retry_state = state

        snapshot = build_runtime_snapshot(app)
        assert snapshot["retry"]["enabled"] is True
        assert snapshot["retry"]["running"] is True
        assert snapshot["retry"]["processed"] == 5
        assert snapshot["retry"]["succeeded"] == 3
        assert snapshot["retry"]["failed"] == 1
        assert snapshot["retry"]["dead_lettered"] == 1


# ---------------------------------------------------------------------------
# 8. Missing adapter during retry — ADAPTER_MISSING classification
# ---------------------------------------------------------------------------


class TestMissingAdapterRetryClassification:
    """When a retry targets an adapter that no longer exists, the
    failure kind is ADAPTER_MISSING — clean, permanent, no crash."""

    def test_classify_missing_adapter(self) -> None:
        """classify_failure returns ADAPTER_MISSING when adapter is not
        registered."""
        from medre.core.planning.delivery_plan import RetryExecutor
        kind = RetryExecutor.classify_failure(
            RuntimeError("adapter gone"),
            adapter_registered=False,
        )
        assert kind is DeliveryFailureKind.ADAPTER_MISSING

    def test_classify_missing_adapter_not_retryable(self) -> None:
        """ADAPTER_MISSING is not retryable."""
        assert DeliveryFailureKind.ADAPTER_MISSING.is_retryable is False

    def test_classify_missing_adapter_overrides_transient(self) -> None:
        """Even a transient error is classified as ADAPTER_MISSING when
        the adapter is not registered."""
        kind = RetryExecutor.classify_failure(
            ConnectionError("network unreachable"),
            adapter_registered=False,
        )
        assert kind is DeliveryFailureKind.ADAPTER_MISSING

    def test_classify_missing_adapter_overrides_adapter_error(self) -> None:
        """AdapterSendError with transient=True is still ADAPTER_MISSING
        when adapter is not registered."""
        from medre.core.contracts.adapter import AdapterSendError
        kind = RetryExecutor.classify_failure(
            AdapterSendError("gone", transient=True),
            adapter_registered=False,
        )
        assert kind is DeliveryFailureKind.ADAPTER_MISSING

    def test_retry_executor_does_not_retry_missing_adapter(self) -> None:
        """RetryExecutor.is_exhausted is irrelevant for ADAPTER_MISSING
        since the failure is permanent, but verify the classification."""
        policy = RetryPolicy(max_attempts=5)
        executor = RetryExecutor(policy)
        kind = RetryExecutor.classify_failure(
            RuntimeError("no adapter"),
            adapter_registered=False,
        )
        assert kind is DeliveryFailureKind.ADAPTER_MISSING
        assert kind.is_retryable is False
