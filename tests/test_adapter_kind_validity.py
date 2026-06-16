"""Glob-based adapter_kind validation for all shipped example configs.

Catches regressions even when a config is not in the required list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "examples" / "configs"


@pytest.mark.parametrize(
    "config_path",
    sorted(CONFIGS_DIR.glob("*.yaml")),
    ids=lambda p: p.name,
)
def test_adapter_kind_glob_valid(config_path: Path) -> None:
    """Every adapter_kind in every shipped YAML config must be real/fake/None."""
    raw = config_path.read_text(encoding="utf-8")
    data = parse_yaml_config(raw)
    if not isinstance(data, dict):
        return
    adapters = data.get("adapters", {})
    if not isinstance(adapters, dict):
        return
    for transport, instances in adapters.items():
        if not isinstance(instances, dict):
            continue
        for inst_name, inst_conf in instances.items():
            if not isinstance(inst_conf, dict):
                continue
            kind = inst_conf.get("adapter_kind", "real")
            assert kind in ("real", "fake"), (
                f"{config_path.name}: adapters.{transport}.{inst_name} "
                f"has invalid adapter_kind={kind!r}"
            )
