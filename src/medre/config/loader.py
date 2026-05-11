"""TOML configuration file loader for MEDRE.

Public API
----------
:func:`load_config`
    Find, read, and parse the TOML config file into a :class:`RuntimeConfig`.
:func:`find_config`
    Search for a config file along the defined search path.
:class:`ConfigSource`
    Enum indicating where the config file was found.
"""
from __future__ import annotations

import os
import tomllib
from enum import Enum
from pathlib import Path
from typing import Self

from medre.config.errors import ConfigError, ConfigNotFoundError, ConfigFileError
from medre.config.model import (
    RuntimeConfig,
    RuntimeOptions,
    LoggingConfig,
    StorageConfig,
    AdapterConfigSet,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    MeshCoreRuntimeConfig,
    LxmfRuntimeConfig,
)
from medre.config.paths import MedrePaths, resolve, MedrePathsError


# ---------------------------------------------------------------------------
# Config source enum
# ---------------------------------------------------------------------------


class ConfigSource(Enum):
    """Indicates how the configuration file was located."""

    EXPLICIT = "explicit"          # --config CLI flag
    MEDRE_CONFIG = "MEDRE_CONFIG"  # $MEDRE_CONFIG env var
    MEDRE_HOME = "MEDRE_HOME"      # $MEDRE_HOME/config.toml
    XDG = "xdg"                    # XDG default config path
    LOCAL = "local"                # ./medre.toml


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------


def find_config(
    explicit_path: str | Path | None = None,
) -> tuple[Path, ConfigSource]:
    """Locate the MEDRE configuration file.

    Search order:

    1. *explicit_path* — if provided, must exist.
    2. ``MEDRE_CONFIG`` environment variable.
    3. ``$MEDRE_HOME/config.toml`` — when ``MEDRE_HOME`` is set.
    4. XDG config path (``~/.config/medre/config.toml`` by default).
    5. ``./medre.toml`` — current working directory.

    Parameters
    ----------
    explicit_path:
        Path provided via ``--config`` CLI flag (or ``None``).

    Returns
    -------
    tuple[Path, ConfigSource]
        The resolved path and its origin.

    Raises
    ------
    ConfigNotFoundError
        If no configuration file could be found.
    ConfigFileError
        If *explicit_path* is provided but does not exist.
    """
    # 1. Explicit path
    if explicit_path is not None:
        p = Path(explicit_path).expanduser().resolve()
        if not p.is_file():
            raise ConfigFileError(
                f"Config file not found: {p} (specified explicitly)"
            )
        return (p, ConfigSource.EXPLICIT)

    checked: list[str] = []

    # 2. MEDRE_CONFIG env var
    medre_config = os.environ.get("MEDRE_CONFIG", "").strip()
    if medre_config:
        p = Path(medre_config).expanduser().resolve()
        if p.is_file():
            return (p, ConfigSource.MEDRE_CONFIG)
        checked.append(f"MEDRE_CONFIG={p}")

    # 3. MEDRE_HOME/config.toml
    medre_home = os.environ.get("MEDRE_HOME", "").strip()
    if medre_home:
        p = Path(medre_home).expanduser().resolve() / "config.toml"
        if p.is_file():
            return (p, ConfigSource.MEDRE_HOME)
        checked.append(f"MEDRE_HOME config={p}")

    # 4. XDG default
    paths = resolve()
    p = paths.config_file
    if p.is_file():
        return (p, ConfigSource.XDG)
    checked.append(f"XDG config={p}")

    # 5. Local ./medre.toml
    p = Path.cwd() / "medre.toml"
    if p.is_file():
        return (p, ConfigSource.LOCAL)
    checked.append(f"local={p}")

    # Nothing found
    raise ConfigNotFoundError(
        "No MEDRE configuration file found. Searched:\n"
        + "\n".join(f"  - {desc}" for desc in checked)
        + "\nGenerate a sample with: medre config sample"
    )


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_config(
    config_path: str | Path | None = None,
) -> tuple[RuntimeConfig, ConfigSource, MedrePaths]:
    """Find, read, and parse the MEDRE TOML configuration.

    Parameters
    ----------
    config_path:
        Optional explicit path (e.g. from ``--config``).  When ``None``,
        :func:`find_config` searches the standard locations.

    Returns
    -------
    tuple[RuntimeConfig, ConfigSource, MedrePaths]
        The parsed configuration, its origin, and the resolved path layout.

    Raises
    ------
    ConfigNotFoundError
        No config file found.
    ConfigFileError
        Config file cannot be read or parsed as valid TOML.
    """
    path, source = find_config(config_path)
    paths = resolve()

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigFileError(f"Cannot read config file {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"Invalid TOML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigFileError(f"Config file {path} did not produce a TOML table")

    config = _parse_runtime_config(data, paths)
    return (config, source, paths)


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _parse_runtime_config(data: dict, paths: MedrePaths) -> RuntimeConfig:
    """Construct a :class:`RuntimeConfig` from a parsed TOML dict."""
    # [runtime] section
    runtime_data = data.get("runtime", {})
    runtime = RuntimeOptions(
        name=runtime_data.get("name", "medre"),
        shutdown_timeout_seconds=runtime_data.get("shutdown_timeout_seconds", 10),
    )

    # [logging] section
    log_data = data.get("logging", {})
    logging = LoggingConfig(
        level=log_data.get("level", "INFO"),
        format=log_data.get("format", "text"),
    )

    # [storage] section — expand path placeholders
    storage_data = data.get("storage", {})
    storage_path = storage_data.get("path")
    if storage_path:
        storage_path = str(paths.expand_placeholder(storage_path))
    storage = StorageConfig(
        backend=storage_data.get("backend", "sqlite"),
        path=storage_path,
    )

    # [adapters.*] sections
    adapters_data = data.get("adapters", {})
    adapters = AdapterConfigSet(
        matrix=_parse_adapter_section(
            adapters_data, "matrix", MatrixRuntimeConfig, paths
        ),
        meshtastic=_parse_adapter_section(
            adapters_data, "meshtastic", MeshtasticRuntimeConfig, paths
        ),
        meshcore=_parse_adapter_section(
            adapters_data, "meshcore", MeshCoreRuntimeConfig, paths
        ),
        lxmf=_parse_adapter_section(
            adapters_data, "lxmf", LxmfRuntimeConfig, paths
        ),
    )

    # Validate adapter config consistency (duplicate IDs, etc.)
    adapters.validate()

    return RuntimeConfig(
        runtime=runtime, logging=logging, storage=storage, adapters=adapters
    )


def _parse_adapter_section(
    data: dict,
    transport: str,
    wrapper_cls: type,
    paths: MedrePaths,
) -> dict[str, object]:
    """Parse an ``[adapters.<transport>]`` section.

    Returns a mapping of *instance_name* → runtime config wrapper instance.
    """
    section = data.get(transport, {})
    result: dict[str, object] = {}
    for instance_name, config_table in section.items():
        if not isinstance(config_table, dict):
            continue
        expanded = _expand_paths_in_dict(config_table, paths)
        wrapper = wrapper_cls.from_toml_dict(instance_name, expanded)
        result[instance_name] = wrapper
    return result


def _expand_paths_in_dict(d: dict, paths: MedrePaths) -> dict:
    """Recursively expand ``{placeholder}`` tokens in string values."""
    result: dict = {}
    for k, v in d.items():
        if isinstance(v, str) and "{" in v:
            try:
                result[k] = str(paths.expand_placeholder(v))
            except MedrePathsError as exc:
                raise ConfigFileError(
                    f"Invalid path placeholder in config field {k!r}: {exc}"
                ) from exc
        elif isinstance(v, dict):
            result[k] = _expand_paths_in_dict(v, paths)
        elif isinstance(v, list):
            result[k] = [
                _expand_paths_in_dict(item, paths) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            result[k] = v
    return result
