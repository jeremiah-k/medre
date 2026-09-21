"""Format-specific tests for :func:`medre.config.loader.load_config`.

General YAML parsing and typed config construction are covered by
``test_config_loader.py`` and route/config domain suites. This module owns the
small format boundary that differs from those tests: ``.yml`` acceptance,
legacy TOML rejection, and unsupported extension rejection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config.errors import ConfigFileError
from medre.config.loader import ConfigSource, load_config
from medre.config.model import RuntimeConfig

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_VALID_YAML = "runtime:\n  name: yml-config\n"


def test_loads_yml_extension(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yml"
    config_path.write_text(_VALID_YAML)

    config, source, _paths = load_config(str(config_path))

    assert isinstance(config, RuntimeConfig)
    assert source == ConfigSource.EXPLICIT
    assert config.runtime.name == "yml-config"


def test_toml_rejection_explains_yaml_replacement(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[runtime]\nname = 'test'\n")

    with pytest.raises(ConfigFileError) as exc_info:
        load_config(str(config_path))

    message = str(exc_info.value)
    assert "TOML config files are no longer supported; use YAML" in message
    assert ".yaml" in message
    assert ".yml" in message


@pytest.mark.parametrize(
    ("suffix", "content"),
    (
        (".txt", "runtime: {}"),
        (".json", '{"runtime": {}}'),
        ("", "runtime: {}"),
    ),
)
def test_unsupported_extension_rejected(
    tmp_path: Path, suffix: str, content: str
) -> None:
    config_path = tmp_path / f"config{suffix}"
    config_path.write_text(content)

    with pytest.raises(ConfigFileError, match="unsupported config file extension"):
        load_config(str(config_path))
