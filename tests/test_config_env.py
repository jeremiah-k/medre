"""Tests for medre.config.env: env var parsing, type coercion,
override application, secrets redaction."""

from __future__ import annotations

import dataclasses

import pytest

from medre.config.env import (
    MedreEnvConfig,
    apply_env_overrides,
    _coerce_bool,
    _coerce_int,
    _coerce_set,
)
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.adapters.matrix.config import MatrixConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(monkeypatch._orig_env.keys() if hasattr(monkeypatch, '_orig_env') else []):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)
    # Also clean the actual env just in case
    import os
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_base_config() -> RuntimeConfig:
    """Create a minimal RuntimeConfig for env override tests."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
    )


def _make_config_with_matrix() -> RuntimeConfig:
    """Create a RuntimeConfig that already has a Matrix adapter."""
    matrix_cfg = MatrixConfig(
        adapter_id="from-toml",
        homeserver="https://matrix.toml",
        user_id="@bot:toml",
        access_token="toml-token",
        encryption_mode="plaintext",
    )
    matrix_rt = MatrixRuntimeConfig(
        adapter_id="from-toml",
        enabled=True,
        config=matrix_cfg,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            matrix={"from-toml": matrix_rt},
        ),
    )


# ---------------------------------------------------------------------------
# Bool coercion
# ---------------------------------------------------------------------------


class TestCoerceBool:
    """_coerce_bool parses boolean env-var values."""

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
    def test_truthy_values(self, value: str) -> None:
        assert _coerce_bool(value, "TEST_VAR") is True

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No", "NO"])
    def test_falsy_values(self, value: str) -> None:
        assert _coerce_bool(value, "TEST_VAR") is False

    def test_invalid_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="boolean"):
            _coerce_bool("maybe", "TEST_VAR")

    def test_empty_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="boolean"):
            _coerce_bool("", "TEST_VAR")

    def test_whitespace_handling(self) -> None:
        assert _coerce_bool("  true  ", "TEST_VAR") is True
        assert _coerce_bool("  false  ", "TEST_VAR") is False


# ---------------------------------------------------------------------------
# Int coercion
# ---------------------------------------------------------------------------


class TestCoerceInt:
    """_coerce_int parses integer env-var values."""

    def test_valid_int(self) -> None:
        assert _coerce_int("42", "TEST_VAR") == 42

    def test_negative_int(self) -> None:
        assert _coerce_int("-5", "TEST_VAR") == -5

    def test_zero(self) -> None:
        assert _coerce_int("0", "TEST_VAR") == 0

    def test_whitespace_stripped(self) -> None:
        assert _coerce_int("  123  ", "TEST_VAR") == 123

    def test_invalid_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="integer"):
            _coerce_int("abc", "TEST_VAR")

    def test_float_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="integer"):
            _coerce_int("3.14", "TEST_VAR")


# ---------------------------------------------------------------------------
# Set coercion (list parsing)
# ---------------------------------------------------------------------------


class TestCoerceSet:
    """_coerce_set parses comma-separated env-var values into sets."""

    def test_basic_comma_separated(self) -> None:
        result = _coerce_set("!room1:test,!room2:test")
        assert result == {"!room1:test", "!room2:test"}

    def test_whitespace_stripped(self) -> None:
        result = _coerce_set("  !room1:test  ,  !room2:test  ")
        assert result == {"!room1:test", "!room2:test"}

    def test_single_value(self) -> None:
        result = _coerce_set("!room:test")
        assert result == {"!room:test"}

    def test_empty_items_discarded(self) -> None:
        result = _coerce_set("!a:test,,!b:test,")
        assert result == {"!a:test", "!b:test"}

    def test_empty_string_produces_empty_set(self) -> None:
        result = _coerce_set("")
        assert result == set()


# ---------------------------------------------------------------------------
# Core overrides
# ---------------------------------------------------------------------------


class TestCoreOverrides:
    """Core MEDRE_DB_PATH and MEDRE_LOG_LEVEL override config fields."""

    def test_db_path_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_DB_PATH", "/custom/path.db")
        base = _make_base_config()
        result = apply_env_overrides(base)
        assert result.storage.path == "/custom/path.db"

    def test_log_level_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "DEBUG")
        base = _make_base_config()
        result = apply_env_overrides(base)
        assert result.logging.level == "DEBUG"

    def test_no_env_vars_returns_same_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Clean all MEDRE_ vars
        import os
        for key in list(os.environ):
            if key.startswith("MEDRE_"):
                monkeypatch.delenv(key, raising=False)

        base = _make_base_config()
        result = apply_env_overrides(base)
        # When no env vars are set, the original config is returned as-is
        assert result is base


# ---------------------------------------------------------------------------
# Matrix adapter overrides
# ---------------------------------------------------------------------------


class TestMatrixOverrides:
    """MEDRE_MATRIX_* env vars override or create Matrix adapter config."""

    def test_homeserver_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://env.matrix.org")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@env:matrix.org")
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "env-tok")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        # Env creates an "env" key adapter
        assert "env" in result.adapters.matrix
        env_matrix = result.adapters.matrix["env"]
        assert env_matrix.config is not None
        assert env_matrix.config.homeserver == "https://env.matrix.org"

    def test_room_allowlist_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://matrix.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@bot:test")
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "tok")
        monkeypatch.setenv("MEDRE_MATRIX_ROOM_ALLOWLIST", "!room1:test,!room2:test")
        base = _make_base_config()
        result = apply_env_overrides(base)

        env_adapter = result.adapters.matrix["env"]
        assert env_adapter.config is not None
        assert isinstance(env_adapter.config.room_allowlist, set)
        assert "!room1:test" in env_adapter.config.room_allowlist
        assert "!room2:test" in env_adapter.config.room_allowlist

    def test_adapter_env_creates_new_instance_when_none_in_toml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no Matrix adapter in TOML, env vars create a new 'env' instance."""
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://new.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@new:test")
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "new-tok")
        base = _make_base_config()  # No adapters
        result = apply_env_overrides(base)

        assert "env" in result.adapters.matrix
        env_adapter = result.adapters.matrix["env"]
        assert env_adapter.config is not None
        assert env_adapter.config.homeserver == "https://new.test"

    def test_enabled_false_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_ENABLED", "false")
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://matrix.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@bot:test")
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "tok")
        base = _make_base_config()
        result = apply_env_overrides(base)

        env_adapter = result.adapters.matrix["env"]
        assert env_adapter.enabled is False


# ---------------------------------------------------------------------------
# Immutability (original config not mutated)
# ---------------------------------------------------------------------------


class TestImmutability:
    """apply_env_overrides returns a new config; original is untouched."""

    def test_original_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "DEBUG")
        base = _make_base_config()
        original_level = base.logging.level

        result = apply_env_overrides(base)

        # Original untouched
        assert base.logging.level == original_level
        # New config has override
        assert result.logging.level == "DEBUG"
        # They are different objects
        assert result is not base
        assert result.logging is not base.logging

    def test_original_adapters_not_mutated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://env.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@env:test")
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "env-tok")
        base = _make_base_config()
        original_matrix_keys = set(base.adapters.matrix.keys())

        result = apply_env_overrides(base)

        assert set(base.adapters.matrix.keys()) == original_matrix_keys
        assert "env" in result.adapters.matrix


# ---------------------------------------------------------------------------
# MedreEnvConfig
# ---------------------------------------------------------------------------


class TestMedreEnvConfig:
    """MedreEnvConfig reads and exposes MEDRE_* env vars."""

    def test_from_environ_captures_known_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_DB_PATH", "/test.db")
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "DEBUG")
        env = MedreEnvConfig.from_environ()
        assert env.db_path == "/test.db"
        assert env.log_level == "DEBUG"

    def test_from_environ_ignores_unknown_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_UNKNOWN_VAR", "whatever")
        env = MedreEnvConfig.from_environ()
        assert env.db_path is None
        assert env.log_level is None

    def test_has_any_set_false_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        for key in list(os.environ):
            if key.startswith("MEDRE_"):
                monkeypatch.delenv(key, raising=False)
        env = MedreEnvConfig.from_environ()
        assert env.has_any_set() is False

    def test_has_any_set_true_when_vars_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_DB_PATH", "/x")
        env = MedreEnvConfig.from_environ()
        assert env.has_any_set() is True

    def test_from_environ_custom_source(self) -> None:
        custom = {"MEDRE_DB_PATH": "/custom.db", "MEDRE_LOG_LEVEL": "TRACE"}
        env = MedreEnvConfig.from_environ(custom)
        assert env.db_path == "/custom.db"
        assert env.log_level == "TRACE"


# ---------------------------------------------------------------------------
# Secrets redaction
# ---------------------------------------------------------------------------


class TestSecretsRedaction:
    """Secret values are redacted in diagnostic output."""

    def test_access_token_redacted_in_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "super-secret-token")
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://matrix.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@bot:test")
        env = MedreEnvConfig.from_environ()

        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_MATRIX_ACCESS_TOKEN"] == "***REDACTED***"
        # Non-secret values remain visible
        assert redacted["MEDRE_MATRIX_HOMESERVER"] == "https://matrix.test"

    def test_redacted_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "secret123")
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://matrix.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@bot:test")
        env = MedreEnvConfig.from_environ()

        r = env.redacted_repr()
        assert "secret123" not in r
        assert "***REDACTED***" in r
        assert "https://matrix.test" in r

    def test_to_dict_contains_unredacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """to_dict returns raw values (unredacted) for programmatic use."""
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "secret123")
        monkeypatch.setenv("MEDRE_MATRIX_HOMESERVER", "https://matrix.test")
        monkeypatch.setenv("MEDRE_MATRIX_USER_ID", "@bot:test")
        env = MedreEnvConfig.from_environ()

        raw = env.to_dict()
        assert raw["MEDRE_MATRIX_ACCESS_TOKEN"] == "secret123"


# ---------------------------------------------------------------------------
# Unknown MEDRE_ env vars
# ---------------------------------------------------------------------------


class TestUnknownEnvVars:
    """Unknown MEDRE_ env vars are handled gracefully."""

    def test_unknown_medre_vars_do_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_FUTURE_FEATURE", "some-value")
        base = _make_base_config()
        # Should not raise — unknown vars are ignored
        result = apply_env_overrides(base)
        assert result is base  # No known vars set, returns same instance
