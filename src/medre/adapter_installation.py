"""Inspect configured adapter requirements before transport-specific config parsing.

Installation is deliberately external to MEDRE's runtime. This preflight reads
only the adapter names in YAML and the installed entry-point registry; it does
not need transport SDKs or assume a package index, Git host, or package name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from medre.adapter_registry import registered_transports
from medre.config._yaml import parse_yaml_config
from medre.config.errors import ConfigFileError, ConfigValidationError
from medre.config.loader import find_config

_TRANSPORT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class AdapterInstallationReport:
    """Declared adapter types versus discoverable, installed providers."""

    config_path: Path
    required: tuple[str, ...]
    installed: tuple[str, ...]
    missing: tuple[str, ...]


def inspect_adapter_installation(
    config_path: str | Path | None,
) -> AdapterInstallationReport:
    """Check transport availability without loading adapter-specific config classes.

    Include disabled instances: a declared transport still needs its config
    model even if runtime startup would not activate that particular instance.
    Normal config validation remains the authoritative check for other fields.
    """
    path, _source = find_config(config_path)
    try:
        data = parse_yaml_config(path.read_text(encoding="utf-8"), str(path))
    except (OSError, UnicodeError) as exc:
        raise ConfigFileError(
            f"Cannot read adapter config {path}: {type(exc).__name__}"
        ) from exc
    adapters = data.get("adapters") or {}
    if not isinstance(adapters, dict):
        raise ConfigValidationError(
            "adapters must be a mapping", section_path="adapters"
        )
    required: list[str] = []
    for transport, instances in adapters.items():
        if not isinstance(transport, str) or not _TRANSPORT_RE.fullmatch(transport):
            raise ConfigValidationError(
                "invalid adapter transport name", section_path="adapters"
            )
        if not isinstance(instances, dict):
            raise ConfigValidationError(
                "adapter instances must be a mapping",
                section_path=f"adapters.{transport}",
            )
        required.append(transport)
    installed = registered_transports()
    missing = tuple(transport for transport in required if transport not in installed)
    return AdapterInstallationReport(path, tuple(required), installed, missing)
