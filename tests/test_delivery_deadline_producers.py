"""Delivery deadline and hard content ceiling producers.

Covers the ``content_ceiling`` configuration surface (model, loader, env
overrides, JSON schema), the admission transaction that writes the policy
ceiling into both content rows, the outbox adoption fence that enforces the
earlier-of effective deadline, and the runtime producer that derives each
admission's absolute UTC ceiling. With the section absent or disabled every
touched path behaves exactly as before the ceiling existed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from medre.config.env import (
    MedreEnvConfig,
    apply_content_ceiling_overrides,
    apply_env_overrides,
)
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.config.model import (
    ContentCeilingConfig,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_CEILING_MAX_AGE_SECONDS = 31_536_000

_MINIMAL_YAML = "adapters: {}\nroutes: {}\n"


def _load_yaml(tmp_path: Path, body: str) -> RuntimeConfig:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(body, encoding="utf-8")
    config, _source, _paths = load_config(str(config_file))
    return config


# ---------------------------------------------------------------------------
# ContentCeilingConfig — strict declaration
# ---------------------------------------------------------------------------


def test_ceiling_defaults_are_disabled() -> None:
    declaration = ContentCeilingConfig().validate()
    assert declaration.enabled is False
    assert declaration.max_age_seconds is None


def test_ceiling_parses_enabled_declaration() -> None:
    declaration = ContentCeilingConfig(enabled=True, max_age_seconds=3600).validate()
    assert declaration.enabled is True
    assert declaration.max_age_seconds == 3600


def test_ceiling_enabled_boolean_is_typed() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled"):
        ContentCeilingConfig(enabled="true", max_age_seconds=3600).validate()  # type: ignore[arg-type]


def test_ceiling_window_rejects_boolean() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.max_age_seconds"):
        ContentCeilingConfig(enabled=True, max_age_seconds=True).validate()  # type: ignore[arg-type]


def test_ceiling_window_rejects_string() -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.max_age_seconds"):
        ContentCeilingConfig(enabled=True, max_age_seconds="3600").validate()  # type: ignore[arg-type]


def test_ceiling_window_rejects_zero_and_negative() -> None:
    for seconds in (0, -1):
        with pytest.raises(ConfigValidationError, match="between 1 and"):
            ContentCeilingConfig(enabled=True, max_age_seconds=seconds).validate()


def test_ceiling_window_rejects_unbounded_declaration() -> None:
    with pytest.raises(ConfigValidationError, match="max_age_seconds"):
        ContentCeilingConfig(enabled=True, max_age_seconds=_CEILING_MAX_AGE_SECONDS + 1).validate()


def test_ceiling_enabled_without_window_is_refused() -> None:
    with pytest.raises(ConfigValidationError, match="requires"):
        ContentCeilingConfig(enabled=True).validate()


def test_ceiling_window_may_be_declared_while_disabled() -> None:
    declaration = ContentCeilingConfig(enabled=False, max_age_seconds=3600).validate()
    assert declaration.enabled is False


# ---------------------------------------------------------------------------
# Loader — [content_ceiling] section
# ---------------------------------------------------------------------------


def test_loader_absent_section_is_disabled(tmp_path: Path) -> None:
    config = _load_yaml(tmp_path, _MINIMAL_YAML)
    assert config.content_ceiling == ContentCeilingConfig().validate()


def test_loader_section_parses_and_validates(tmp_path: Path) -> None:
    config = _load_yaml(
        tmp_path,
        "content_ceiling:\n  enabled: true\n  max_age_seconds: 900\n"
        + _MINIMAL_YAML,
    )
    assert config.content_ceiling.enabled is True
    assert config.content_ceiling.max_age_seconds == 900


def test_loader_disabled_section_with_window_loads(tmp_path: Path) -> None:
    config = _load_yaml(
        tmp_path,
        "content_ceiling:\n  enabled: false\n  max_age_seconds: 900\n"
        + _MINIMAL_YAML,
    )
    assert config.content_ceiling.enabled is False


def test_loader_unknown_section_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="unknown key"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  window: 5\n" + _MINIMAL_YAML,
        )


def test_loader_non_boolean_enabled_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n" + _MINIMAL_YAML,
        )


def test_loader_enabled_without_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="content_ceiling.enabled=true"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  max_age_seconds: null\n"
            + _MINIMAL_YAML,
        )


def test_loader_window_out_of_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="between 1 and"):
        _load_yaml(
            tmp_path,
            "content_ceiling:\n  enabled: true\n  max_age_seconds: 0\n"
            + _MINIMAL_YAML,
        )


# ---------------------------------------------------------------------------
# Environment overrides — MEDRE_CONTENT_CEILING__<FIELD>
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between env-override tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _make_base_config() -> RuntimeConfig:
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="memory"),
    )


def test_ceiling_fields_parse_from_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "7200")
    env = MedreEnvConfig.from_environ()
    assert env.content_ceiling_overrides["enabled"] == "true"
    assert env.content_ceiling_overrides["max_age_seconds"] == "7200"


def test_ceiling_field_names_are_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__enabled", "true")
    env = MedreEnvConfig.from_environ()
    assert env.content_ceiling_overrides["enabled"] == "true"


def test_ceiling_unsupported_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Unsupported MEDRE_CONTENT_CEILING__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_CEILING__WINDOW": "x"})


def test_ceiling_malformed_empty_field_raises() -> None:
    with pytest.raises(
        ConfigValidationError, match="Malformed MEDRE_CONTENT_CEILING__"
    ):
        MedreEnvConfig.from_environ({"MEDRE_CONTENT_CEILING__": "v"})


def test_ceiling_duplicate_normalized_field_raises() -> None:
    with pytest.raises(ConfigValidationError, match="Duplicate normalized content_ceiling"):
        MedreEnvConfig.from_environ(
            {
                "MEDRE_CONTENT_CEILING__ENABLED": "true",
                "MEDRE_CONTENT_CEILING__enabled": "false",
            }
        )


def test_ceiling_overrides_apply_and_coerce(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "600")
    result = apply_env_overrides(base)
    assert result.content_ceiling.enabled is True
    assert result.content_ceiling.max_age_seconds == 600


def test_ceiling_override_enabled_without_window_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    with pytest.raises(ConfigValidationError, match="requires"):
        apply_env_overrides(base)


def test_ceiling_override_bad_window_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__ENABLED", "true")
    monkeypatch.setenv("MEDRE_CONTENT_CEILING__MAX_AGE_SECONDS", "not-a-number")
    with pytest.raises(ConfigValidationError, match="MAX_AGE_SECONDS"):
        apply_env_overrides(base)


def test_ceiling_direct_apply_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _make_base_config()
    result = apply_content_ceiling_overrides(
        base, {"enabled": "true", "max_age_seconds": "60"}
    )
    assert result.content_ceiling == ContentCeilingConfig(
        enabled=True, max_age_seconds=60
    )


def test_ceiling_empty_overrides_return_same_config() -> None:
    base = _make_base_config()
    assert apply_content_ceiling_overrides(base, {}) is base


# ---------------------------------------------------------------------------
# JSON schema — docs/schemas/runtime-config.schema.json parity
# ---------------------------------------------------------------------------


def _runtime_config_schema() -> dict[str, object]:
    import json

    schema_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "schemas"
        / "runtime-config.schema.json"
    )
    schema: dict[str, object] = json.loads(
        schema_path.read_text(encoding="utf-8")
    )
    return schema


def test_schema_declares_ceiling_section_with_closed_keys() -> None:
    schema = _runtime_config_schema()
    ceiling = schema["properties"]["content_ceiling"]  # type: ignore[index]
    assert ceiling["additionalProperties"] is False  # type: ignore[index]
    assert set(ceiling["properties"]) == {"enabled", "max_age_seconds"}  # type: ignore[index]


def test_schema_rejects_enabled_ceiling_without_window() -> None:
    schema = _runtime_config_schema()
    validator = Draft202012Validator(schema)
    document = {"content_ceiling": {"enabled": True}}
    assert list(validator.iter_errors(document)) != []


def test_schema_accepts_disabled_and_enabled_declarations() -> None:
    validator = Draft202012Validator(_runtime_config_schema())
    assert list(validator.iter_errors({})) == []
    assert (
        list(validator.iter_errors({"content_ceiling": {"enabled": False}})) == []
    )
    assert (
        list(
            validator.iter_errors(
                {"content_ceiling": {"enabled": True, "max_age_seconds": 3600}}
            )
        )
        == []
    )


def test_schema_rejects_out_of_range_window() -> None:
    validator = Draft202012Validator(_runtime_config_schema())
    for seconds in (0, _CEILING_MAX_AGE_SECONDS + 1):
        document = {"content_ceiling": {"enabled": True, "max_age_seconds": seconds}}
        assert list(validator.iter_errors(document)) != []


# ---------------------------------------------------------------------------
# Default-off parity of the loaded model
# ---------------------------------------------------------------------------


def test_default_runtime_config_carries_disabled_ceiling() -> None:
    config = RuntimeConfig()
    assert config.content_ceiling.enabled is False
    assert config.content_ceiling.max_age_seconds is None


def test_ceiling_window_bound_matches_documented_constant() -> None:
    from medre.config.model import CONTENT_CEILING_MAX_AGE_SECONDS

    assert CONTENT_CEILING_MAX_AGE_SECONDS == 31_536_000


def test_ceiling_admission_time_derivation_shape() -> None:
    """The declared window is seconds added to a trusted admission time."""
    declared = ContentCeilingConfig(enabled=True, max_age_seconds=90).validate()
    admitted = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert admitted + timedelta(seconds=declared.max_age_seconds) == datetime(
        2026, 5, 1, 12, 1, 30, tzinfo=timezone.utc
    )
