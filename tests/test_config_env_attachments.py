"""Tests for MEDRE_ATTACHMENTS__<FIELD> env var parsing and override application.

Covers:
- Field parsing (ENABLED, MAX_ATTACHMENT_BYTES, MAX_RETAINED_BYTES,
  MAX_CONCURRENT_TRANSFERS, TRANSFER_TIMEOUT_SECONDS)
- Case-insensitive field names
- Malformed / unsupported / duplicate field errors
- Type coercion and whole-section revalidation through apply_env_overrides
- Isolation from retry / adapter / route overrides
"""

from __future__ import annotations

import os

import pytest

from medre.config.env import (
    MedreEnvConfig,
    apply_attachment_overrides,
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
    """Create a minimal RuntimeConfig for attachments env override tests."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="memory"),
    )


# ---------------------------------------------------------------------------
# MEDRE_ATTACHMENTS__ env var parsing
# ---------------------------------------------------------------------------


class TestAttachmentsEnvOverrides:
    """MEDRE_ATTACHMENTS__<FIELD> overrides AttachmentConfig fields."""

    def test_attachments_enabled_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "true")
        env = MedreEnvConfig.from_environ()
        assert env.attachments_overrides["enabled"] == "true"

    def test_attachments_max_attachment_bytes_int(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES", "2048")
        env = MedreEnvConfig.from_environ()
        assert env.attachments_overrides["max_attachment_bytes"] == "2048"

    def test_attachments_transfer_timeout_seconds_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ATTACHMENTS__TRANSFER_TIMEOUT_SECONDS", "2.5")
        env = MedreEnvConfig.from_environ()
        assert env.attachments_overrides["transfer_timeout_seconds"] == "2.5"

    def test_attachments_case_insensitive_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Field names in MEDRE_ATTACHMENTS__ are case-insensitive."""
        monkeypatch.setenv("MEDRE_ATTACHMENTS__enabled", "true")
        env = MedreEnvConfig.from_environ()
        assert env.attachments_overrides["enabled"] == "true"

    def test_attachments_unsupported_field_raises(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="Unsupported MEDRE_ATTACHMENTS__"
        ):
            MedreEnvConfig.from_environ({"MEDRE_ATTACHMENTS__UNKNOWN_FIELD": "x"})

    def test_attachments_malformed_empty_raises(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="Malformed MEDRE_ATTACHMENTS__"
        ):
            MedreEnvConfig.from_environ({"MEDRE_ATTACHMENTS__": "v"})

    def test_attachments_malformed_extra_separator_raises(self) -> None:
        with pytest.raises(
            ConfigValidationError, match="Malformed MEDRE_ATTACHMENTS__"
        ):
            MedreEnvConfig.from_environ({"MEDRE_ATTACHMENTS__ENABLED__EXTRA": "v"})

    def test_attachments_duplicate_normalized_raises(self) -> None:
        """Two MEDRE_ATTACHMENTS__ vars normalizing to same field raise."""
        with pytest.raises(
            ConfigValidationError, match="Duplicate normalized attachments"
        ):
            MedreEnvConfig.from_environ(
                {
                    "MEDRE_ATTACHMENTS__ENABLED": "true",
                    "MEDRE_ATTACHMENTS__enabled": "false",
                }
            )

    def test_attachments_overrides_do_not_affect_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDRE_ATTACHMENTS__ vars don't interfere with retry parsing."""
        monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "true")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES", "2048")
        env = MedreEnvConfig.from_environ()
        assert env.retry_overrides == {}
        assert env.instance_overrides == {}
        assert env.route_overrides == {}

    def test_attachments_coerces_to_attachment_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apply_env_overrides with MEDRE_ATTACHMENTS__ vars produces correct values."""
        base = _make_base_config()
        monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "true")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES", "2048")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_RETAINED_BYTES", "4096")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_CONCURRENT_TRANSFERS", "3")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__TRANSFER_TIMEOUT_SECONDS", "2.5")
        result = apply_env_overrides(base)
        assert result.attachments.enabled is True
        assert result.attachments.max_attachment_bytes == 2048
        assert result.attachments.max_retained_bytes == 4096
        assert result.attachments.max_concurrent_transfers == 3
        assert result.attachments.transfer_timeout_seconds == 2.5

    def test_attachments_invalid_int_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES", "not-an-int")
        base = _make_base_config()
        with pytest.raises(
            ConfigValidationError, match="MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES"
        ):
            apply_env_overrides(base)

    def test_attachments_invalid_bool_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "not-a-bool")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="MEDRE_ATTACHMENTS__ENABLED"):
            apply_env_overrides(base)

    def test_attachments_cross_field_revalidation_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A per-attachment cap above the retained budget fails whole-section
        revalidation even though each value coerces individually."""
        base = _make_base_config()
        monkeypatch.setenv("MEDRE_ATTACHMENTS__ENABLED", "true")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_ATTACHMENT_BYTES", "9000")
        monkeypatch.setenv("MEDRE_ATTACHMENTS__MAX_RETAINED_BYTES", "1000")
        with pytest.raises(ConfigValidationError, match="max_attachment_bytes"):
            apply_env_overrides(base)

    def test_apply_attachment_overrides_empty_returns_same_config(self) -> None:
        base = _make_base_config()
        assert apply_attachment_overrides(base, {}) is base

    def test_apply_attachment_overrides_rejects_unknown_field(self) -> None:
        """A field outside AttachmentConfig fails closed at apply time."""
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="Unknown attachments field"):
            apply_attachment_overrides(base, {"bogus_field": "1"})

    def test_apply_attachment_overrides_enables_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct application coerces and revalidates the section as a whole."""
        base = _make_base_config()
        result = apply_attachment_overrides(
            base,
            {
                "enabled": "true",
                "max_attachment_bytes": "512",
                "max_retained_bytes": "1024",
            },
        )
        assert result.attachments.enabled is True
        assert result.attachments.max_attachment_bytes == 512
        assert result.attachments.max_retained_bytes == 1024
