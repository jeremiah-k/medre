"""Tests for MEDRE_RETRY__<FIELD> env var parsing and override application.

Covers:
- Field parsing (ENABLED, INTERVAL_SECONDS, BATCH_SIZE, MAX_ATTEMPTS)
- Case-insensitive field names
- Malformed / unsupported field errors
- Type coercion through apply_env_overrides
- Isolation from adapter / route overrides
"""

from __future__ import annotations

import os

import pytest

from medre.config.env import (
    MedreEnvConfig,
    apply_env_overrides,
)
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_base_config() -> RuntimeConfig:
    """Create a minimal RuntimeConfig for retry env override tests."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="memory"),
    )


# ---------------------------------------------------------------------------
# MEDRE_RETRY__ env var parsing
# ---------------------------------------------------------------------------


class TestRetryEnvOverrides:
    """MEDRE_RETRY__<FIELD> overrides RetryConfig fields."""

    def test_retry_enabled_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_RETRY__ENABLED", "true")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides["enabled"] == "true"

    def test_retry_max_attempts_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_RETRY__MAX_ATTEMPTS", "5")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides["max_attempts"] == "5"

    def test_retry_interval_seconds_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_RETRY__INTERVAL_SECONDS", "5.5")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides["interval_seconds"] == "5.5"

    def test_retry_batch_size_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_RETRY__BATCH_SIZE", "10")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides["batch_size"] == "10"

    def test_retry_unsupported_field_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="Unsupported MEDRE_RETRY__"):
            MedreEnvConfig.from_environ({"MEDRE_RETRY__UNKNOWN_FIELD": "x"})

    def test_retry_malformed_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_RETRY__"):
            MedreEnvConfig.from_environ({"MEDRE_RETRY__": "v"})

    def test_retry_too_many_parts_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed MEDRE_RETRY__"):
            MedreEnvConfig.from_environ({"MEDRE_RETRY__ENABLED__EXTRA": "v"})

    def test_retry_coerces_to_retry_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apply_env_overrides with MEDRE_RETRY__ vars produces correct RetryConfig."""
        base = _make_base_config()
        monkeypatch.setenv("MEDRE_RETRY__ENABLED", "true")
        monkeypatch.setenv("MEDRE_RETRY__MAX_ATTEMPTS", "5")
        monkeypatch.setenv("MEDRE_RETRY__INTERVAL_SECONDS", "15.0")
        monkeypatch.setenv("MEDRE_RETRY__BATCH_SIZE", "10")
        result = apply_env_overrides(base)
        assert result.retry.enabled is True
        assert result.retry.max_attempts == 5
        assert result.retry.interval_seconds == 15.0
        assert result.retry.batch_size == 10

    def test_retry_case_insensitive_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field names in MEDRE_RETRY__ are case-insensitive."""
        monkeypatch.setenv("MEDRE_RETRY__enabled", "true")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides["enabled"] == "true"

    def test_retry_does_not_affect_adapters_or_routes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDRE_RETRY__ vars don't interfere with adapter/route parsing."""
        monkeypatch.setenv("MEDRE_RETRY__ENABLED", "true")
        monkeypatch.setenv("MEDRE_RETRY__MAX_ATTEMPTS", "5")
        env = MedreEnvConfig.from_environ()
        assert env.instance_overrides == {}
        assert env.route_overrides == {}

    def test_retry_invalid_bool_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_RETRY__ENABLED", "not-a-bool")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="MEDRE_RETRY__ENABLED"):
            apply_env_overrides(base)

    def test_retry_invalid_int_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_RETRY__MAX_ATTEMPTS", "not-an-int")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="MEDRE_RETRY__MAX_ATTEMPTS"):
            apply_env_overrides(base)
