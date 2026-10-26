"""Operator startup deadlines remain finite, positive and schema-aligned."""

import json
from pathlib import Path

import jsonschema
import pytest

from medre.config.errors import ConfigValidationError
from medre.config.loader import parse_runtime_config
from medre.config.model import RuntimeOptions
from medre.config.paths import resolve


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "30", None])
def test_invalid_adapter_start_deadline_fails_validation(value):
    """Invalid deadlines fail at configuration, before opening runtime resources."""
    with pytest.raises(ConfigValidationError, match="adapter_start_timeout_seconds"):
        RuntimeOptions(adapter_start_timeout_seconds=value).validate()


def test_adapter_start_deadline_parses_and_matches_schema(tmp_path):
    """The operator can lengthen setup without changing delivery or shutdown limits."""
    document = {"runtime": {"adapter_start_timeout_seconds": 75.5}}
    config = parse_runtime_config(
        document, resolve(environ={"MEDRE_HOME": str(tmp_path)})
    )
    assert config.runtime.adapter_start_timeout_seconds == 75.5
    assert config.runtime.shutdown_timeout_seconds == 10
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/runtime-config.schema.json"
        ).read_text()
    )
    jsonschema.validate(document, schema)
    assert (
        RuntimeOptions().adapter_start_timeout_seconds
        == schema["properties"]["runtime"]["properties"][
            "adapter_start_timeout_seconds"
        ]["default"]
    )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_invalid_environment_start_deadline_is_rejected(tmp_path, monkeypatch, value):
    """Environment configuration cannot bypass finite positive validation."""
    from medre.config.env import apply_env_overrides

    paths = resolve(environ={"MEDRE_HOME": str(tmp_path)})
    config = parse_runtime_config({}, paths)
    monkeypatch.setenv("MEDRE_RUNTIME_ADAPTER_START_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigValidationError):
        apply_env_overrides(config, paths)


def test_environment_start_deadline_overrides_yaml(tmp_path, monkeypatch):
    """An explicit deployment override changes startup without touching delivery."""
    from medre.config.env import apply_env_overrides

    paths = resolve(environ={"MEDRE_HOME": str(tmp_path)})
    config = parse_runtime_config(
        {"runtime": {"adapter_start_timeout_seconds": 40}}, paths
    )
    monkeypatch.setenv("MEDRE_RUNTIME_ADAPTER_START_TIMEOUT_SECONDS", "75.5")
    overridden = apply_env_overrides(config, paths)
    assert overridden.runtime.adapter_start_timeout_seconds == 75.5
    assert config.runtime.adapter_start_timeout_seconds == 40
    assert overridden.limits == config.limits
