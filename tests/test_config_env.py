"""Tests for medre.config.env: env var parsing, type coercion,
instance-scoped adapter overrides, token normalization, secrets redaction."""

from __future__ import annotations

import pytest

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.env import (
    MedreEnvConfig,
    _coerce_bool,
    _coerce_int,
    _coerce_set,
    apply_env_overrides,
    detect_token_collisions,
    normalize_adapter_id,
)
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
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


def _make_config_with_two_matrix() -> RuntimeConfig:
    """RuntimeConfig with two Matrix adapters (primary and secondary)."""
    primary_cfg = MatrixConfig(
        adapter_id="matrix-primary",
        homeserver="https://primary.toml",
        user_id="@bot:primary",
        access_token="primary-token",
        encryption_mode="plaintext",
    )
    secondary_cfg = MatrixConfig(
        adapter_id="matrix-secondary",
        homeserver="https://secondary.toml",
        user_id="@bot:secondary",
        access_token="secondary-token",
        encryption_mode="plaintext",
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            matrix={
                "matrix-primary": MatrixRuntimeConfig(
                    adapter_id="matrix-primary",
                    enabled=True,
                    config=primary_cfg,
                ),
                "matrix-secondary": MatrixRuntimeConfig(
                    adapter_id="matrix-secondary",
                    enabled=True,
                    config=secondary_cfg,
                ),
            },
        ),
    )


def _make_config_with_matrix_and_meshtastic() -> RuntimeConfig:
    """RuntimeConfig with one Matrix and one Meshtastic adapter."""
    matrix_cfg = MatrixConfig(
        adapter_id="matrix-main",
        homeserver="https://matrix.toml",
        user_id="@bot:toml",
        access_token="mat-token",
        encryption_mode="plaintext",
    )
    meshtastic_cfg = MeshtasticConfig(
        adapter_id="radio-a",
        connection_type="tcp",
        host="192.168.1.100",
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            matrix={
                "matrix-main": MatrixRuntimeConfig(
                    adapter_id="matrix-main",
                    config=matrix_cfg,
                ),
            },
            meshtastic={
                "radio-a": MeshtasticRuntimeConfig(
                    adapter_id="radio-a",
                    config=meshtastic_cfg,
                ),
            },
        ),
    )


def _make_config_with_meshcore() -> RuntimeConfig:
    """RuntimeConfig with a MeshCore adapter using BLE."""
    meshcore_cfg = MeshCoreConfig(
        adapter_id="mc-ble",
        connection_type="ble",
        ble_address="AA:BB:CC:DD:EE:FF",
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            meshcore={
                "mc-ble": MeshCoreRuntimeConfig(
                    adapter_id="mc-ble",
                    config=meshcore_cfg,
                ),
            },
        ),
    )


def _make_config_with_lxmf() -> RuntimeConfig:
    """RuntimeConfig with an LXMF adapter."""
    lxmf_cfg = LxmfConfig(
        adapter_id="lxmf-receiver",
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            lxmf={
                "lxmf-receiver": LxmfRuntimeConfig(
                    adapter_id="lxmf-receiver",
                    config=lxmf_cfg,
                ),
            },
        ),
    )


def _make_config_with_meshtastic_tcp() -> RuntimeConfig:
    """RuntimeConfig with a Meshtastic TCP adapter (has port field)."""
    meshtastic_cfg = MeshtasticConfig(
        adapter_id="radio-tcp",
        connection_type="tcp",
        host="192.168.1.50",
        port=4403,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            meshtastic={
                "radio-tcp": MeshtasticRuntimeConfig(
                    adapter_id="radio-tcp",
                    config=meshtastic_cfg,
                ),
            },
        ),
    )


def _make_config_with_colliding_adapters() -> RuntimeConfig:
    """Config where radio-a and radio_a both normalize to RADIO_A."""
    m1 = MeshtasticConfig(adapter_id="radio-a", connection_type="fake")
    m2 = MeshtasticConfig(adapter_id="radio_a", connection_type="fake")
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(
            meshtastic={
                "radio-a": MeshtasticRuntimeConfig(adapter_id="radio-a", config=m1),
                "radio_a": MeshtasticRuntimeConfig(adapter_id="radio_a", config=m2),
            },
        ),
    )


# ---------------------------------------------------------------------------
# Bool coercion
# ---------------------------------------------------------------------------


class TestCoerceBool:
    """_coerce_bool parses boolean env-var values."""

    @pytest.mark.parametrize(
        "value", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"]
    )
    def test_truthy_values(self, value: str) -> None:
        assert _coerce_bool(value, "TEST_VAR") is True

    @pytest.mark.parametrize(
        "value", ["false", "False", "FALSE", "0", "no", "No", "NO"]
    )
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

    def test_no_env_vars_returns_same_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
# Adapter token normalization
# ---------------------------------------------------------------------------


class TestNormalizeAdapterId:
    """normalize_adapter_id converts adapter_id strings to env tokens."""

    @pytest.mark.parametrize(
        "adapter_id, expected",
        [
            ("matrix-primary", "MATRIX_PRIMARY"),
            ("matrix_primary", "MATRIX_PRIMARY"),
            ("radio.a", "RADIO_A"),
            ("meshcore/tbeam", "MESHCORE_TBEAM"),
            ("lxmf_receiver", "LXMF_RECEIVER"),
            ("simple", "SIMPLE"),
            ("already_upper", "ALREADY_UPPER"),
        ],
    )
    def test_normalization(self, adapter_id: str, expected: str) -> None:
        assert normalize_adapter_id(adapter_id) == expected


# ---------------------------------------------------------------------------
# Token collisions
# ---------------------------------------------------------------------------


class TestTokenCollisions:
    """detect_token_collisions raises on ambiguous adapter IDs."""

    def test_collision_detected(self) -> None:
        """radio-a and radio_a both normalize to RADIO_A — must raise."""
        adapters = {"radio-a": object(), "radio_a": object()}
        with pytest.raises(ConfigValidationError, match="normalize to RADIO_A"):
            detect_token_collisions(adapters)

    def test_no_collision_different_tokens(self) -> None:
        """radio-a and radio_b produce different tokens — no error."""
        adapters = {"radio-a": object(), "radio_b": object()}
        detect_token_collisions(adapters)  # should not raise

    def test_collision_detected_without_env_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token collision raises even when no env vars are set."""
        import os

        for key in list(os.environ):
            if key.startswith("MEDRE_"):
                monkeypatch.delenv(key, raising=False)
        base = _make_config_with_colliding_adapters()
        with pytest.raises(
            ConfigValidationError, match="token collision for RADIO_A"
        ) as exc_info:
            apply_env_overrides(base)
        msg = str(exc_info.value)
        assert "radio-a" in msg
        assert "radio_a" in msg

    def test_exact_duplicate_cross_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same adapter_id across matrix and meshtastic raises collision."""
        import os

        for key in list(os.environ):
            if key.startswith("MEDRE_"):
                monkeypatch.delenv(key, raising=False)

        matrix_cfg = MatrixConfig(
            adapter_id="shared",
            homeserver="https://matrix.test",
            user_id="@bot:test",
            access_token="tok",
            encryption_mode="plaintext",
        )
        meshtastic_cfg = MeshtasticConfig(
            adapter_id="shared",
            connection_type="fake",
        )
        config = RuntimeConfig(
            runtime=RuntimeOptions(name="test"),
            logging=LoggingConfig(level="INFO"),
            storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
            adapters=AdapterConfigSet(
                matrix={
                    "shared": MatrixRuntimeConfig(
                        adapter_id="shared",
                        config=matrix_cfg,
                    ),
                },
                meshtastic={
                    "shared": MeshtasticRuntimeConfig(
                        adapter_id="shared",
                        config=meshtastic_cfg,
                    ),
                },
            ),
        )
        with pytest.raises(ConfigValidationError, match="SHARED"):
            apply_env_overrides(config)


# ---------------------------------------------------------------------------
# Matrix adapter overrides (instance-scoped)
# ---------------------------------------------------------------------------


class TestMatrixOverrides:
    """MEDRE_ADAPTER__FROM_TOML__<FIELD> overrides existing Matrix adapter."""

    def test_homeserver_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://env.matrix.org")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        assert "from-toml" in result.adapters.matrix
        env_matrix = result.adapters.matrix["from-toml"]
        assert env_matrix.config is not None
        assert env_matrix.config.homeserver == "https://env.matrix.org"

    def test_room_allowlist_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FROM_TOML__room_allowlist",
            "!roomA:example.com,!roomB:example.com",
        )
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        env_adapter = result.adapters.matrix["from-toml"]
        assert env_adapter.config is not None
        assert isinstance(env_adapter.config.room_allowlist, set)
        assert "!roomA:example.com" in env_adapter.config.room_allowlist
        assert "!roomB:example.com" in env_adapter.config.room_allowlist

    def test_enabled_false_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__enabled", "false")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        env_adapter = result.adapters.matrix["from-toml"]
        assert env_adapter.enabled is False

    def test_access_token_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__access_token", "syt_new_token")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        env_adapter = result.adapters.matrix["from-toml"]
        assert env_adapter.config is not None
        assert env_adapter.config.access_token == "syt_new_token"


# ---------------------------------------------------------------------------
# Instance-scoped overrides
# ---------------------------------------------------------------------------


class TestInstanceScopedOverrides:
    """MEDRE_ADAPTER__<TOKEN>__<FIELD> overrides per adapter instance."""

    def test_one_adapter_one_field_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single adapter, single field override."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://override.test")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        assert result.adapters.matrix["from-toml"].config.homeserver == "https://override.test"
        # Other fields unchanged
        assert result.adapters.matrix["from-toml"].config.user_id == "@bot:toml"

    def test_two_adapters_same_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two Matrix adapters, each gets its own override."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_PRIMARY__homeserver", "https://primary-env.test")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SECONDARY__homeserver", "https://secondary-env.test")
        base = _make_config_with_two_matrix()
        result = apply_env_overrides(base)

        assert result.adapters.matrix["matrix-primary"].config.homeserver == "https://primary-env.test"
        assert result.adapters.matrix["matrix-secondary"].config.homeserver == "https://secondary-env.test"

    def test_two_adapters_different_transports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matrix and Meshtastic adapters, each with own override."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__homeserver", "https://env-matrix.test")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__host", "10.0.0.1")
        base = _make_config_with_matrix_and_meshtastic()
        result = apply_env_overrides(base)

        assert result.adapters.matrix["matrix-main"].config.homeserver == "https://env-matrix.test"
        assert result.adapters.meshtastic["radio-a"].config.host == "10.0.0.1"

    def test_meshcore_ble_address_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MeshCore BLE address override via env var."""
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__ble_address", "11:22:33:44:55:66")
        base = _make_config_with_meshcore()
        result = apply_env_overrides(base)

        assert result.adapters.meshcore["mc-ble"].config.ble_address == "11:22:33:44:55:66"

    def test_lxmf_identity_path_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LXMF identity path override via env var."""
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_RECEIVER__identity_path", "/path/to/identity")
        base = _make_config_with_lxmf()
        result = apply_env_overrides(base)

        assert result.adapters.lxmf["lxmf-receiver"].config.identity_path == "/path/to/identity"

    def test_set_field_override_room_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """set[str] field (room_allowlist) parsed from comma-separated value."""
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FROM_TOML__room_allowlist",
            "!roomA:example.com,!roomB:example.com",
        )
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        allowlist = result.adapters.matrix["from-toml"].config.room_allowlist
        assert allowlist == {"!roomA:example.com", "!roomB:example.com"}

    def test_bool_field_override_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Boolean enabled field parsed from env var."""
        base = _make_config_with_matrix()

        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__enabled", "false")
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].enabled is False

        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__enabled", "true")
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].enabled is True

    def test_int_field_override_sync_timeout_ms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Integer sync_timeout_ms field parsed from env var."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__sync_timeout_ms", "60000")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)

        assert result.adapters.matrix["from-toml"].config.sync_timeout_ms == 60000


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    """Instance-scoped env var errors raise ConfigValidationError."""

    def test_unknown_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Token with no matching adapter lists known tokens."""
        monkeypatch.setenv("MEDRE_ADAPTER__NONEXISTENT__homeserver", "https://nope.test")
        base = _make_config_with_matrix()

        with pytest.raises(ConfigValidationError, match="Unknown adapter token"):
            apply_env_overrides(base)

    def test_unknown_token_error_lists_known_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error message includes known tokens for diagnostics."""
        monkeypatch.setenv("MEDRE_ADAPTER__NONEXISTENT__homeserver", "x")
        base = _make_config_with_matrix()

        with pytest.raises(ConfigValidationError, match="Known tokens.*FROM_TOML"):
            apply_env_overrides(base)

    def test_unsupported_field_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown field name on a valid adapter raises with valid field names."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__totally_fake_field", "x")
        base = _make_config_with_matrix()

        with pytest.raises(ConfigValidationError, match="Unsupported fields"):
            apply_env_overrides(base)

    def test_unsupported_field_error_lists_valid_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error message includes valid fields for the transport."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__totally_fake_field", "x")
        base = _make_config_with_matrix()

        with pytest.raises(ConfigValidationError, match="Valid fields"):
            apply_env_overrides(base)

    def test_invalid_bool_coercion_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid bool value includes env var name in error."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__enabled", "maybe")
        base = _make_config_with_matrix()

        with pytest.raises(
            ConfigValidationError, match="MEDRE_ADAPTER__FROM_TOML__enabled"
        ):
            apply_env_overrides(base)

    def test_invalid_int_coercion_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid int value includes env var name in error."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__sync_timeout_ms", "not-a-number")
        base = _make_config_with_matrix()

        with pytest.raises(
            ConfigValidationError, match="MEDRE_ADAPTER__FROM_TOML__sync_timeout_ms"
        ):
            apply_env_overrides(base)


# ---------------------------------------------------------------------------
# Provenance and redaction
# ---------------------------------------------------------------------------


class TestProvenanceAndRedaction:
    """EnvProvenance tracks env vars and redacts secret fields."""

    def test_access_token_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """access_token is redacted in provenance output."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__access_token", "syt_super_secret")
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://matrix.test")
        env = MedreEnvConfig.from_environ()

        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_ADAPTER__FROM_TOML__access_token"] == "***REDACTED***"

    def test_secret_field_patterns_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fields matching TOKEN/SECRET/PASSWORD/KEY/AUTH/CREDENTIAL are redacted."""
        for field_name in ("access_token", "SECRET", "PASSWORD", "KEY", "AUTH", "CREDENTIAL"):
            monkeypatch.setenv(f"MEDRE_ADAPTER__FROM_TOML__{field_name}", "secret-val")
        env = MedreEnvConfig.from_environ()

        redacted = dict(env.provenance.redacted_items())
        for field_name in ("access_token", "SECRET", "PASSWORD", "KEY", "AUTH", "CREDENTIAL"):
            assert redacted[f"MEDRE_ADAPTER__FROM_TOML__{field_name}"] == "***REDACTED***"

    def test_homeserver_not_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-secret fields remain visible."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://matrix.test")
        env = MedreEnvConfig.from_environ()

        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_ADAPTER__FROM_TOML__homeserver"] == "https://matrix.test"

    def test_redacted_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """redacted_repr() hides secrets but shows non-secret values."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__access_token", "secret123")
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://matrix.test")
        env = MedreEnvConfig.from_environ()

        r = env.redacted_repr()
        assert "secret123" not in r
        assert "***REDACTED***" in r
        assert "https://matrix.test" in r

    def test_to_dict_contains_unredacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """to_dict returns raw values (unredacted) for programmatic use."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__access_token", "secret123")
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://matrix.test")
        env = MedreEnvConfig.from_environ()

        raw = env.to_dict()
        assert raw["MEDRE_ADAPTER__FROM_TOML__access_token"] == "secret123"

    # -- FIX 7: Instance-aware provenance -----------------------------------

    def test_instance_override_provenance_includes_adapter_id_and_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Instance override provenance records adapter_id and field."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://env.test")
        env = MedreEnvConfig.from_environ()
        instance_entries = [
            e for e in env.provenance.entries if e.source_kind == "instance"
        ]
        assert len(instance_entries) == 1
        entry = instance_entries[0]
        assert entry.target_adapter_token == "FROM_TOML"
        assert entry.target_field == "homeserver"

    def test_core_env_provenance_no_transport_or_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core env provenance has no transport/field info."""
        monkeypatch.setenv("MEDRE_DB_PATH", "/test.db")
        env = MedreEnvConfig.from_environ()
        core_entries = [
            e for e in env.provenance.entries if e.source_kind == "core"
        ]
        assert len(core_entries) == 1
        entry = core_entries[0]
        assert entry.target_adapter_token is None
        assert entry.target_transport is None
        assert entry.target_field is None

    # -- FIX 8: Expanded redaction ------------------------------------------

    def test_ble_address_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BLE address is redacted (matches BLE pattern)."""
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__ble_address", "AA:BB:CC:DD:EE:FF")
        env = MedreEnvConfig.from_environ()
        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_ADAPTER__MC_BLE__ble_address"] == "***REDACTED***"

    def test_identity_path_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """identity_path is redacted (matches IDENTITY pattern)."""
        monkeypatch.setenv(
            "MEDRE_ADAPTER__LXMF_RECEIVER__identity_path", "/path/to/identity"
        )
        env = MedreEnvConfig.from_environ()
        redacted = dict(env.provenance.redacted_items())
        assert (
            redacted["MEDRE_ADAPTER__LXMF_RECEIVER__identity_path"]
            == "***REDACTED***"
        )

    def test_homeserver_host_port_not_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HOMESERVER, HOST, PORT fields are NOT redacted."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://m.test")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__host", "10.0.0.1")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__port", "4403")
        env = MedreEnvConfig.from_environ()
        redacted = dict(env.provenance.redacted_items())
        assert redacted["MEDRE_ADAPTER__FROM_TOML__homeserver"] == "https://m.test"
        assert redacted["MEDRE_ADAPTER__RADIO_A__host"] == "10.0.0.1"
        assert redacted["MEDRE_ADAPTER__RADIO_A__port"] == "4403"


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

    def test_original_adapters_not_mutated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://env.test")
        base = _make_config_with_matrix()
        original_hs = base.adapters.matrix["from-toml"].config.homeserver

        result = apply_env_overrides(base)

        assert base.adapters.matrix["from-toml"].config.homeserver == original_hs
        assert result.adapters.matrix["from-toml"].config.homeserver == "https://env.test"


# ---------------------------------------------------------------------------
# MedreEnvConfig
# ---------------------------------------------------------------------------


class TestMedreEnvConfig:
    """MedreEnvConfig reads and exposes MEDRE_* env vars."""

    def test_from_environ_captures_known_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_DB_PATH", "/test.db")
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "DEBUG")
        env = MedreEnvConfig.from_environ()
        assert env.db_path == "/test.db"
        assert env.log_level == "DEBUG"

    def test_core_vars_still_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Core vars (MEDRE_HOME, MEDRE_CONFIG, MEDRE_DB_PATH, etc.) captured."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        monkeypatch.setenv("MEDRE_CONFIG", "/etc/medre/medre.toml")
        monkeypatch.setenv("MEDRE_DB_PATH", "/data/medre.db")
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "TRACE")
        env = MedreEnvConfig.from_environ()
        assert env.home == "/opt/medre"
        assert env.config_path == "/etc/medre/medre.toml"
        assert env.db_path == "/data/medre.db"
        assert env.log_level == "TRACE"

    def test_adapter_token_field_vars_in_instance_overrides(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADAPTER__TOKEN__FIELD vars stored in instance_overrides."""
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__homeserver", "https://env.test")
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__access_token", "tok")
        env = MedreEnvConfig.from_environ()
        overrides = env.instance_overrides["FROM_TOML"]
        assert overrides["homeserver"].raw_value == "https://env.test"
        assert overrides["access_token"].raw_value == "tok"

    def test_unknown_medre_vars_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown MEDRE_* vars don't crash and aren't captured."""
        monkeypatch.setenv("MEDRE_FUTURE_FEATURE", "some-value")
        env = MedreEnvConfig.from_environ()
        assert env.db_path is None
        assert env.log_level is None
        assert env.instance_overrides == {}

    def test_has_any_set_false_when_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        for key in list(os.environ):
            if key.startswith("MEDRE_"):
                monkeypatch.delenv(key, raising=False)
        env = MedreEnvConfig.from_environ()
        assert env.has_any_set() is False

    def test_has_any_set_true_when_vars_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_DB_PATH", "/x")
        env = MedreEnvConfig.from_environ()
        assert env.has_any_set() is True

    def test_from_environ_custom_source(self) -> None:
        custom = {"MEDRE_DB_PATH": "/custom.db", "MEDRE_LOG_LEVEL": "TRACE"}
        env = MedreEnvConfig.from_environ(custom)
        assert env.db_path == "/custom.db"
        assert env.log_level == "TRACE"

    def test_from_environ_custom_source_with_adapter_vars(self) -> None:
        custom = {
            "MEDRE_ADAPTER__RADIO_A__host": "10.0.0.1",
            "MEDRE_ADAPTER__RADIO_A__port": "4403",
        }
        env = MedreEnvConfig.from_environ(custom)
        overrides = env.instance_overrides["RADIO_A"]
        assert overrides["host"].raw_value == "10.0.0.1"
        assert overrides["port"].raw_value == "4403"


# ---------------------------------------------------------------------------
# Unknown MEDRE_ env vars
# ---------------------------------------------------------------------------


class TestUnknownEnvVars:
    """Unknown MEDRE_ env vars are handled gracefully."""

    def test_unknown_medre_vars_do_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_FUTURE_FEATURE", "some-value")
        base = _make_base_config()
        # Should not raise — unknown vars are ignored
        result = apply_env_overrides(base)
        assert result is base  # No known vars set, returns same instance


# ---------------------------------------------------------------------------
# FIX 1: Case-insensitive field names
# ---------------------------------------------------------------------------


class TestCaseInsensitiveFields:
    """Field names in MEDRE_ADAPTER__ vars are case-insensitive."""

    def test_uppercase_field_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FROM_TOML__HOMESERVER", "https://upper.test"
        )
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].config.homeserver == "https://upper.test"

    def test_lowercase_field_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FROM_TOML__homeserver", "https://lower.test"
        )
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].config.homeserver == "https://lower.test"

    def test_mixed_case_field_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__Room_Allowlist", "!room:test")
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].config.room_allowlist == {
            "!room:test",
        }


# ---------------------------------------------------------------------------
# FIX 2: Legacy transport env vars rejected
# ---------------------------------------------------------------------------


class TestRejectedLegacyEnvVars:
    """Legacy transport-specific env vars raise with migration guidance."""

    def test_matrix_access_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "tok")
        with pytest.raises(ConfigValidationError, match="Legacy transport env variable"):
            MedreEnvConfig.from_environ()

    def test_meshtastic_serial_port_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_MESHTASTIC_SERIAL_PORT", "/dev/ttyUSB0")
        with pytest.raises(ConfigValidationError, match="Legacy transport env variable"):
            MedreEnvConfig.from_environ()

    def test_meshcore_ble_address_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_MESHCORE_BLE_ADDRESS", "AA:BB:CC:DD:EE:FF")
        with pytest.raises(ConfigValidationError, match="Legacy transport env variable"):
            MedreEnvConfig.from_environ()

    def test_lxmf_identity_path_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_LXMF_IDENTITY_PATH", "/path/to/id")
        with pytest.raises(ConfigValidationError, match="Legacy transport env variable"):
            MedreEnvConfig.from_environ()

    def test_medre_home_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MEDRE_HOME is a core var, not a legacy transport var."""
        monkeypatch.setenv("MEDRE_HOME", "/opt/medre")
        env = MedreEnvConfig.from_environ()
        assert env.home == "/opt/medre"

    def test_error_includes_migration_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "tok")
        with pytest.raises(ConfigValidationError, match="MEDRE_ADAPTER__"):
            MedreEnvConfig.from_environ()


# ---------------------------------------------------------------------------
# FIX 3: Optional/PEP-604 type coercion
# ---------------------------------------------------------------------------


class TestOptionalTypeCoercion:
    """Optional types (int | None, set[str] | None) are unwrapped and coerced."""

    def test_meshtastic_port_int_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Meshtastic port (int | None) — coerced to int when unwrap works."""
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__port", "5503")
        base = _make_config_with_meshtastic_tcp()
        result = apply_env_overrides(base)
        coerced_port = result.adapters.meshtastic["radio-tcp"].config.port
        # Verify the override was applied (coercion depends on
        # _unwrap_optional_type handling PEP-604 X | None).
        assert coerced_port == 5503
        assert isinstance(coerced_port, int)

    def test_meshcore_port_int_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MeshCore port (int | None) — coerced to int when unwrap works."""
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__port", "8080")
        base = _make_config_with_meshcore()
        result = apply_env_overrides(base)
        coerced_port = result.adapters.meshcore["mc-ble"].config.port
        assert coerced_port == 8080
        assert isinstance(coerced_port, int)

    def test_matrix_room_allowlist_set_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Matrix room_allowlist (set[str] | None) becomes set[str] from env."""
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FROM_TOML__room_allowlist", "!a:test,!b:test"
        )
        base = _make_config_with_matrix()
        result = apply_env_overrides(base)
        assert result.adapters.matrix["from-toml"].config.room_allowlist == {
            "!a:test",
            "!b:test",
        }
        assert isinstance(
            result.adapters.matrix["from-toml"].config.room_allowlist, set
        )

    def test_invalid_optional_int_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid int value for optional int field raises ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__port", "not-a-number")
        base = _make_config_with_meshtastic_tcp()
        with pytest.raises(ConfigValidationError):
            apply_env_overrides(base)


# ---------------------------------------------------------------------------
# FIX 4: Dict/tuple field rejection
# ---------------------------------------------------------------------------


class TestUnsupportedFieldTypes:
    """Dict and tuple fields cannot be set through env vars."""

    def test_dict_field_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__channel_mapping", "0:chan0")
        base = _make_config_with_matrix_and_meshtastic()
        with pytest.raises(ConfigValidationError, match="dict") as exc_info:
            apply_env_overrides(base)
        assert "cannot be set through env" in str(exc_info.value)

    def test_tuple_field_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__FROM_TOML__auto_join_rooms", "!room:test")
        base = _make_config_with_matrix()
        with pytest.raises(ConfigValidationError, match="tuple") as exc_info:
            apply_env_overrides(base)
        assert "cannot be set through env" in str(exc_info.value)


# ---------------------------------------------------------------------------
# FIX 5: Malformed MEDRE_ADAPTER vars
# ---------------------------------------------------------------------------


class TestMalformedAdapterEnvVars:
    """Malformed MEDRE_ADAPTER__ vars raise with expected shape format."""

    def test_empty_after_prefix(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed"):
            MedreEnvConfig.from_environ({"MEDRE_ADAPTER__": "v"})

    def test_no_field(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed"):
            MedreEnvConfig.from_environ({"MEDRE_ADAPTER__MAIN": "v"})

    def test_empty_token(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed"):
            MedreEnvConfig.from_environ({"MEDRE_ADAPTER____HOST": "v"})

    def test_empty_field(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed"):
            MedreEnvConfig.from_environ({"MEDRE_ADAPTER__MAIN__": "v"})

    def test_too_many_parts_raises(self) -> None:
        with pytest.raises(ConfigValidationError, match="Malformed"):
            MedreEnvConfig.from_environ(
                {"MEDRE_ADAPTER__MAIN__HOST__EXTRA": "v"}
            )

    def test_error_includes_expected_shape(self) -> None:
        with pytest.raises(ConfigValidationError, match="Expected shape"):
            MedreEnvConfig.from_environ({"MEDRE_ADAPTER__": "v"})

    def test_unrelated_env_var_ignored(self) -> None:
        """Non-ADAPTER MEDRE_ vars don't cause malformed errors."""
        env = MedreEnvConfig.from_environ({"MEDRE_FUTURE_FEATURE": "some-value"})
        assert env.instance_overrides == {}


# ---------------------------------------------------------------------------
# Env-first adapter creation
# ---------------------------------------------------------------------------


class TestEnvCreatedAdapters:
    """Env tokens with TRANSPORT field create new adapters from scratch."""

    # (a) Single Matrix adapter created from env.
    def test_create_matrix_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__HOMESERVER", "https://matrix.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__USER_ID", "@bot:env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_MAIN__ACCESS_TOKEN", "env-token")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-main" in result.adapters.matrix
        adapter = result.adapters.matrix["matrix-main"]
        assert adapter.adapter_id == "matrix-main"
        assert adapter.enabled is True
        assert adapter.config.homeserver == "https://matrix.env"
        assert adapter.config.user_id == "@bot:env"
        assert adapter.config.access_token == "env-token"

    # (b) Two Matrix adapters created from env.
    def test_create_two_matrix_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__HOMESERVER", "https://a.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__USER_ID", "@bot:a")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_A__ACCESS_TOKEN", "tok-a")

        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__HOMESERVER", "https://b.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__USER_ID", "@bot:b")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_B__ACCESS_TOKEN", "tok-b")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-a" in result.adapters.matrix
        assert "matrix-b" in result.adapters.matrix
        assert result.adapters.matrix["matrix-a"].config.homeserver == "https://a.env"
        assert result.adapters.matrix["matrix-b"].config.homeserver == "https://b.env"

    # (c) Meshtastic serial adapter created from env.
    def test_create_meshtastic_serial_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__CONNECTION_TYPE", "serial")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_SERIAL__SERIAL_PORT", "/dev/ttyUSB0")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-serial" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-serial"]
        assert adapter.adapter_id == "radio-serial"
        assert adapter.config.connection_type == "serial"
        assert adapter.config.serial_port == "/dev/ttyUSB0"

    # (d) Meshtastic TCP adapter created from env.
    def test_create_meshtastic_tcp_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_TCP__HOST", "192.168.1.100")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-tcp" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-tcp"]
        assert adapter.config.connection_type == "tcp"
        assert adapter.config.host == "192.168.1.100"

    # (e) MeshCore BLE adapter created from env.
    def test_create_meshcore_ble_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__TRANSPORT", "meshcore")
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__CONNECTION_TYPE", "ble")
        monkeypatch.setenv("MEDRE_ADAPTER__MC_BLE__BLE_ADDRESS", "AA:BB:CC:DD:EE:FF")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "mc-ble" in result.adapters.meshcore
        adapter = result.adapters.meshcore["mc-ble"]
        assert adapter.config.connection_type == "ble"
        assert adapter.config.ble_address == "AA:BB:CC:DD:EE:FF"

    # (f) LXMF adapter created from env.
    def test_create_lxmf_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__LXMF_MAIN__TRANSPORT", "lxmf")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "lxmf-main" in result.adapters.lxmf
        adapter = result.adapters.lxmf["lxmf-main"]
        assert adapter.adapter_id == "lxmf-main"
        assert adapter.config.connection_type == "fake"

    # (g) Unknown token without TRANSPORT raises with new error format.
    def test_unknown_token_without_transport_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__ORPHAN__HOMESERVER", "https://nope.test")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="Unknown adapter token"):
            apply_env_overrides(base)

    # (h) Invalid TRANSPORT value raises with supported transports.
    def test_invalid_transport_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__BAD__TRANSPORT", "carrier-pigeon")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD__HOMESERVER", "https://x.test")

        base = _make_base_config()
        with pytest.raises(
            ConfigValidationError, match="Invalid TRANSPORT"
        ) as exc_info:
            apply_env_overrides(base)
        msg = str(exc_info.value)
        assert "carrier-pigeon" in msg
        assert "matrix" in msg

    # (i) ENABLED=false works for created adapter.
    def test_created_adapter_enabled_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__HOMESERVER", "https://matrix.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__USER_ID", "@bot:off")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__ACCESS_TOKEN", "tok")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_OFF__ENABLED", "false")

        base = _make_base_config()
        result = apply_env_overrides(base)

        adapter = result.adapters.matrix["matrix-off"]
        assert adapter.enabled is False

    # (j) Explicit ADAPTER_ID works for new env adapter.
    def test_created_adapter_explicit_adapter_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__ADAPTER_ID", "my-custom-id")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__HOMESERVER", "https://custom.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__USER_ID", "@bot:custom")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_X__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "my-custom-id" in result.adapters.matrix
        assert result.adapters.matrix["my-custom-id"].adapter_id == "my-custom-id"

    # (k) Two env-created adapters resolving to same adapter_id raises.
    def test_created_adapter_id_collision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__ADAPTER_ID", "shared")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__HOMESERVER", "https://a.env")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__USER_ID", "@bot:a")
        monkeypatch.setenv("MEDRE_ADAPTER__ALPHA__ACCESS_TOKEN", "tok-a")

        monkeypatch.setenv("MEDRE_ADAPTER__BETA__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__BETA__ADAPTER_ID", "shared")
        monkeypatch.setenv("MEDRE_ADAPTER__BETA__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="collision.*SHARED"):
            apply_env_overrides(base)

    # (l) Provenance redacts ACCESS_TOKEN for created adapter.
    def test_created_adapter_secrets_redacted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__HOMESERVER", "https://matrix.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__USER_ID", "@bot:sec")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_SEC__ACCESS_TOKEN", "super-secret")

        env = MedreEnvConfig.from_environ()
        redacted = dict(env.provenance.redacted_items())
        assert (
            redacted["MEDRE_ADAPTER__MATRIX_SEC__ACCESS_TOKEN"]
            == "***REDACTED***"
        )
        assert (
            redacted["MEDRE_ADAPTER__MATRIX_SEC__HOMESERVER"]
            == "https://matrix.env"
        )

    # (m) TOML radio-a + env-created radio_a collision raises.
    def test_toml_and_env_created_normalized_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TOML adapter_id='radio-a' and env-created adapter_id='radio_a'
        both normalize to RADIO_A — must raise."""
        base = _make_config_with_matrix_and_meshtastic()

        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__ADAPTER_ID", "radio_a")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A2__CONNECTION_TYPE", "fake")

        with pytest.raises(ConfigValidationError, match="RADIO_A"):
            apply_env_overrides(base)

    # (n) Two env-created adapters with normalizing adapter_ids collision raises.
    def test_env_created_normalized_collision(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two env-created adapters whose adapter_ids normalize to same token."""
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")

        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__ADAPTER_ID", "radio_a")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="RADIO_A"):
            apply_env_overrides(base)

    # (o) Env-created Matrix adapter with ADAPTER_KIND=fake.
    def test_created_matrix_adapter_kind_fake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER", "https://fake.env")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__USER_ID", "@bot:fake")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "matrix-fake" in result.adapters.matrix
        adapter = result.adapters.matrix["matrix-fake"]
        assert adapter.adapter_kind == "fake"

    # (p) Env-created Meshtastic adapter with ADAPTER_KIND=fake.
    def test_created_meshtastic_adapter_kind_fake(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__ADAPTER_KIND", "fake")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_FAKE__CONNECTION_TYPE", "fake")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "radio-fake" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["radio-fake"]
        assert adapter.adapter_kind == "fake"

    # (q) Invalid ADAPTER_KIND raises.
    def test_invalid_adapter_kind_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__ADAPTER_KIND", "invalid")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__HOMESERVER", "https://bad.env")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__USER_ID", "@bot:bad")
        monkeypatch.setenv("MEDRE_ADAPTER__BAD_KIND__ACCESS_TOKEN", "tok")

        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="ADAPTER_KIND"):
            apply_env_overrides(base)

    # (r) Default adapter_id becomes map key on created adapter.
    def test_default_adapter_id_is_map_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__MY_RADIO__HOST", "10.0.0.1")

        base = _make_base_config()
        result = apply_env_overrides(base)

        # Default adapter_id from token MY_RADIO → "my-radio"
        assert "my-radio" in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["my-radio"]
        assert adapter.adapter_id == "my-radio"

    # (s) Explicit ADAPTER_ID becomes map key on created adapter.
    def test_explicit_adapter_id_is_map_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__ADAPTER_ID", "custom-radio")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__CONNECTION_TYPE", "tcp")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_X__HOST", "10.0.0.1")

        base = _make_base_config()
        result = apply_env_overrides(base)

        assert "custom-radio" in result.adapters.meshtastic
        assert "radio-x" not in result.adapters.meshtastic
        adapter = result.adapters.meshtastic["custom-radio"]
        assert adapter.adapter_id == "custom-radio"

    # (t) Routes remain TOML-only.
    def test_routes_remain_toml_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env overrides do not affect routes; routes must be declared in TOML."""
        base = _make_config_with_matrix_and_meshtastic()
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__HOST", "10.0.0.99")

        result = apply_env_overrides(base)

        # Routes are unchanged — no env var can create or modify routes.
        assert result.routes == base.routes

    # (u) Unsupported field validation for env-created adapters.
    def test_env_created_unsupported_field_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unsupported fields on env-created adapters raise ConfigValidationError."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_BAD__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_BAD__UNSUPPORTED_FIELD", "x")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError) as excinfo:
            apply_env_overrides(base)
        msg = str(excinfo.value)
        assert "unsupported_field" in msg

    # (v) Dict/tuple field rejection for env-created adapters.
    def test_env_created_adapter_rejects_dict_tuple_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dict/tuple fields rejected on env-created adapters."""
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__TRANSPORT", "matrix")
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__HOMESERVER", "https://env.test")
        # auto_join_rooms is a tuple field — should be rejected
        monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_ENV__auto_join_rooms", "!room:test")
        base = _make_base_config()
        with pytest.raises(ConfigValidationError, match="cannot be set through env"):
            apply_env_overrides(base)
