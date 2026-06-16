"""YAML configuration file loader for MEDRE.

Public API
----------
:func:`load_config`
    Find, read, and parse the YAML config file into a :class:`RuntimeConfig`.
:func:`find_config`
    Search for a config file along the defined search path.
:class:`ConfigSource`
    Enum indicating where the config file was found.
"""

from __future__ import annotations

import os
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Self, TypeVar

from medre.config._yaml import StrictYAMLError, parse_yaml_config
from medre.config.errors import (
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RetryConfig,
    RuntimeConfig,
    RuntimeLimits,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError, resolve
from medre.config.routes import RouteConfigSet
from medre.core.observability.log_levels import VALID_LEVEL_NAMES

# ---------------------------------------------------------------------------
# Config source enum
# ---------------------------------------------------------------------------


class ConfigSource(Enum):
    """Indicates how the configuration file was located."""

    EXPLICIT = "explicit"  # --config CLI flag
    MEDRE_CONFIG = "MEDRE_CONFIG"  # $MEDRE_CONFIG env var
    MEDRE_HOME = "MEDRE_HOME"  # $MEDRE_HOME/config.yaml
    XDG = "xdg"  # XDG default config path
    LOCAL = "local"  # ./medre.yaml


# ---------------------------------------------------------------------------
# Extension / format guards
# ---------------------------------------------------------------------------

#: File suffixes accepted as YAML config.
_SUPPORTED_YAML_SUFFIXES: frozenset[str] = frozenset({".yaml", ".yml"})

#: File suffix that used to be supported and now must produce a clear error.
_TOML_SUFFIX = ".toml"

#: Exact message operators see when pointing at a TOML config file.
_TOML_NOT_SUPPORTED_MSG = (
    "TOML config files are no longer supported; use YAML (.yaml or .yml)."
)

#: Top-level keys accepted at the root of a MEDRE config file.
#: Matches :class:`medre.config.model.RuntimeConfig` sections. Any key not
#: in this set is rejected at load time so typos (e.g. ``roues:``) surface
#: as a :class:`ConfigValidationError` instead of silently being dropped.
_KNOWN_ROOT_KEYS: frozenset[str] = frozenset(
    {"runtime", "logging", "storage", "retry", "adapters", "routes"}
)


def _validate_config_suffix(path: Path) -> None:
    """Ensure *path* has a supported YAML extension.

    Raises
    ------
    ConfigFileError
        If the suffix is ``.toml`` (with the dedicated migration message) or
        any other unsupported extension.
    """
    suffix = path.suffix.lower()
    if suffix in _SUPPORTED_YAML_SUFFIXES:
        return
    if suffix == _TOML_SUFFIX:
        raise ConfigFileError(f"{path}: {_TOML_NOT_SUPPORTED_MSG}")
    raise ConfigFileError(
        f"{path}: unsupported config file extension {suffix!r}; " f"use .yaml or .yml"
    )


def _discover_yaml(directory: Path, basename: str) -> Path | None:
    """Find ``{basename}.yaml`` or ``{basename}.yml`` in *directory*.

    ``.yaml`` is preferred over ``.yml``.  Returns ``None`` if neither
    file exists.
    """
    yaml_path = directory / f"{basename}.yaml"
    if yaml_path.is_file():
        return yaml_path
    yml_path = directory / f"{basename}.yml"
    if yml_path.is_file():
        return yml_path
    return None


def _raise_if_legacy_toml(directory: Path) -> None:
    """Raise :class:`ConfigFileError` if a legacy TOML config exists in *directory*.

    Operators who still have a ``config.toml`` or ``medre.toml`` left over
    from the historical TOML config format get a clear migration pointer
    instead of a silent "not found" when YAML discovery misses.  Both
    historical basenames are checked so the hint fires regardless of which
    naming convention the operator used.
    """
    for basename in ("config", "medre"):
        toml_path = directory / f"{basename}{_TOML_SUFFIX}"
        if toml_path.is_file():
            raise ConfigFileError(f"{toml_path}: {_TOML_NOT_SUPPORTED_MSG}")


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------


def find_config(
    explicit_path: str | Path | None = None,
) -> tuple[Path, ConfigSource]:
    """Locate the MEDRE configuration file.

    Search order:

    1. *explicit_path* — if provided, must exist and have a supported
       YAML extension (``.yaml`` or ``.yml``).
    2. ``MEDRE_CONFIG`` environment variable.
    3. ``$MEDRE_HOME/config.yaml`` (or ``.yml``) — when ``MEDRE_HOME`` is
       set.
    4. XDG config path (``~/.config/medre/config.yaml`` by default, or
       ``.yml``).
    5. ``./medre.yaml`` (or ``./medre.yml``) — current working directory.

    ``.yaml`` is preferred over ``.yml`` in every auto-discovery step.
    A ``.toml`` file found via an explicit path, ``MEDRE_CONFIG``, or any
    of the auto-discovery locations (``MEDRE_HOME``, XDG config dir, local
    cwd) is rejected with :data:`_TOML_NOT_SUPPORTED_MSG`.

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
        If *explicit_path* is provided but does not exist, or if any
        discovered path has an unsupported extension.
    """
    # 1. Explicit path
    if explicit_path is not None:
        p = Path(explicit_path).expanduser().resolve()
        if not p.is_file():
            raise ConfigFileError(f"Config file not found: {p} (specified explicitly)")
        _validate_config_suffix(p)
        return (p, ConfigSource.EXPLICIT)

    checked: list[str] = []

    # 2. MEDRE_CONFIG env var
    medre_config = os.environ.get("MEDRE_CONFIG", "").strip()
    if medre_config:
        p = Path(medre_config).expanduser().resolve()
        if p.is_file():
            _validate_config_suffix(p)
            return (p, ConfigSource.MEDRE_CONFIG)
        checked.append(f"MEDRE_CONFIG={p}")

    # 3. MEDRE_HOME/config.yaml (or .yml)
    medre_home = os.environ.get("MEDRE_HOME", "").strip()
    if medre_home:
        home = Path(medre_home).expanduser().resolve()
        found = _discover_yaml(home, "config")
        if found is not None:
            return (found, ConfigSource.MEDRE_HOME)
        _raise_if_legacy_toml(home)
        checked.append(f"MEDRE_HOME config={home / 'config.yaml'}")

    # 4. XDG default
    paths = resolve()
    if paths.config_dir is not None:
        found = _discover_yaml(paths.config_dir, "config")
        if found is not None:
            return (found, ConfigSource.XDG)
        _raise_if_legacy_toml(paths.config_dir)
    checked.append(f"XDG config={paths.config_file}")

    # 5. Local ./medre.yaml (or .yml)
    found = _discover_yaml(Path.cwd(), "medre")
    if found is not None:
        return (found, ConfigSource.LOCAL)
    _raise_if_legacy_toml(Path.cwd())
    checked.append(f"local={Path.cwd() / 'medre.yaml'}")

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
    """Find, read, and parse the MEDRE YAML configuration.

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
        Config file cannot be read or parsed as valid YAML, or the file
        has an unsupported extension (e.g. ``.toml``).
    """
    path, source = find_config(config_path)
    paths = replace(resolve(), config_file=path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"Cannot read config file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # read_text decodes the file as UTF-8; a non-UTF-8 config is wrapped
        # as ConfigFileError rather than leaking the raw codec error.
        raise ConfigFileError(f"Config file {path} is not valid UTF-8: {exc}") from exc

    try:
        data = parse_yaml_config(raw, str(path))
    except StrictYAMLError:
        # StrictYAMLError already inherits ConfigFileError and carries a
        # clean, secret-safe message.
        raise

    config = _parse_runtime_config(data, paths)
    return (config, source, paths)


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _parse_runtime_config(data: dict, paths: MedrePaths) -> RuntimeConfig:
    """Construct a :class:`RuntimeConfig` from a parsed YAML dict.

    The *data* argument is a plain ``dict`` produced by the strict YAML
    parser; the runtime config construction is format-agnostic and feeds
    plain dict/list/scalar values into the typed config dataclasses.
    """
    # [runtime] section
    runtime_data = data.get("runtime", {})
    runtime = RuntimeOptions(
        name=runtime_data.get("name", "medre"),
        shutdown_timeout_seconds=runtime_data.get("shutdown_timeout_seconds", 10),
    )

    # [runtime.limits] section (nested under [runtime])
    limits_data = runtime_data.get("limits", {})
    limits = RuntimeLimits(
        max_inflight_deliveries=limits_data.get("max_inflight_deliveries", 100),
        max_inflight_replay_events=limits_data.get("max_inflight_replay_events", 100),
        shutdown_drain_timeout_seconds=limits_data.get(
            "shutdown_drain_timeout_seconds", 10
        ),
        delivery_acquire_timeout_seconds=limits_data.get(
            "delivery_acquire_timeout_seconds", 1.0
        ),
    ).validate()

    # [logging] section — validate raw data before constructing LoggingConfig
    log_data = data.get("logging", {})
    _validate_logging_section(log_data)

    # Canonicalise level/format/overrides so downstream consumers always
    # receive normalised values regardless of source config casing.
    canonical_level = log_data.get("level", "INFO").upper()
    canonical_format = log_data.get("format", "text").lower()
    raw_overrides = log_data.get("overrides", {})
    canonical_overrides = (
        {k: v.upper() for k, v in raw_overrides.items()}
        if raw_overrides
        else raw_overrides
    )

    logging = LoggingConfig(
        level=canonical_level,
        format=canonical_format,
        overrides=canonical_overrides,
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

    # [retry] section — validated for operator-friendly errors
    retry_data = data.get("retry", {})
    _validate_retry_section(retry_data)
    retry = RetryConfig(
        enabled=retry_data.get("enabled", False),
        interval_seconds=retry_data.get("interval_seconds", 10.0),
        batch_size=retry_data.get("batch_size", 20),
        max_attempts=retry_data.get("max_attempts", 3),
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
        lxmf=_parse_adapter_section(adapters_data, "lxmf", LxmfRuntimeConfig, paths),
    )

    # Validate adapter config consistency (duplicate IDs, etc.)
    adapters.validate()

    # [routes.*] sections
    routes = RouteConfigSet.from_dict(data)

    # Reject unknown root-level keys so operator typos (e.g. ``roues:``)
    # surface as a clear error rather than silently dropping the section.
    # Matches ``additionalProperties: false`` on the JSON schemas.
    unknown_root = set(data) - _KNOWN_ROOT_KEYS
    if unknown_root:
        raise ConfigValidationError(
            f"Unknown root config key(s): "
            f"{sorted(unknown_root, key=lambda k: (type(k).__name__, repr(k)))}. "
            f"Valid keys are: {sorted(_KNOWN_ROOT_KEYS)}.",
            section_path="<root>",
        )

    return RuntimeConfig(
        runtime=runtime,
        logging=logging,
        storage=storage,
        limits=limits,
        retry=retry,
        adapters=adapters,
        routes=routes,
    )


def _validate_retry_section(retry_data: dict) -> None:
    """Validate [retry] section fields for type and range.

    Produces clean ``ConfigValidationError`` messages so operators get
    actionable feedback without reading source code.
    """
    _RETRY_INT_FIELDS = {
        "batch_size": ("batch size", 1),
        "max_attempts": ("max attempts", 1),
    }
    _RETRY_FLOAT_FIELDS = {
        "interval_seconds": ("polling interval", 0.0),
    }
    _RETRY_BOOL_FIELDS = {"enabled"}

    for field_name, (_label, min_val) in _RETRY_INT_FIELDS.items():
        raw = retry_data.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise ConfigValidationError(
                f"[retry] {field_name} must be an integer, got {type(raw).__name__} "
                f"{raw!r}",
                section_path="retry",
            )
        if raw < min_val:
            raise ConfigValidationError(
                f"[retry] {field_name} must be >= {min_val}, got {raw}",
                section_path="retry",
            )

    for field_name, (_label, min_val) in _RETRY_FLOAT_FIELDS.items():
        raw = retry_data.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ConfigValidationError(
                f"[retry] {field_name} must be a number, got {type(raw).__name__} "
                f"{raw!r}",
                section_path="retry",
            )
        if float(raw) <= min_val:
            raise ConfigValidationError(
                f"[retry] {field_name} must be > {min_val}, got {raw}",
                section_path="retry",
            )

    for field_name in _RETRY_BOOL_FIELDS:
        raw = retry_data.get(field_name)
        if raw is None:
            continue
        if not isinstance(raw, bool):
            raise ConfigValidationError(
                f"[retry] {field_name} must be a boolean (true/false), "
                f"got {type(raw).__name__} {raw!r}",
                section_path="retry",
            )


def _validate_logging_section(log_data: dict) -> None:
    """Validate [logging] section types and values from raw config data.

    Validates that *level*, *format*, and *overrides* have correct types
    and permissible values **before** constructing :class:`LoggingConfig`,
    so that misconfiguration is caught at config-load time with clear
    :class:`ConfigValidationError` messages rather than deferred to
    :func:`setup_logging` or producing ``AttributeError``.
    """
    _VALID_FORMATS: frozenset[str] = frozenset({"text", "json"})

    if not isinstance(log_data, dict):
        raise ConfigValidationError(
            f"[logging] must be a table, got {type(log_data).__name__}",
            section_path="logging",
        )

    # --- level ---
    raw_level = log_data.get("level", "INFO")
    if not isinstance(raw_level, str):
        raise ConfigValidationError(
            f"[logging] level must be a string, "
            f"got {type(raw_level).__name__} {raw_level!r}",
            section_path="logging",
        )
    if raw_level.upper() not in VALID_LEVEL_NAMES:
        raise ConfigValidationError(
            f"[logging] level must be one of "
            f"{', '.join(sorted(VALID_LEVEL_NAMES))}, "
            f"got {raw_level!r}",
            section_path="logging",
        )

    # --- format ---
    raw_format = log_data.get("format", "text")
    if not isinstance(raw_format, str):
        raise ConfigValidationError(
            f"[logging] format must be a string, "
            f"got {type(raw_format).__name__} {raw_format!r}",
            section_path="logging",
        )
    if raw_format.lower() not in _VALID_FORMATS:
        raise ConfigValidationError(
            f"[logging] format must be one of "
            f"{', '.join(sorted(_VALID_FORMATS))}, "
            f"got {raw_format!r}",
            section_path="logging",
        )

    # --- overrides ---
    raw_overrides = log_data.get("overrides")
    if raw_overrides is not None:
        if not isinstance(raw_overrides, dict):
            raise ConfigValidationError(
                f"[logging] overrides must be a table, "
                f"got {type(raw_overrides).__name__}",
                section_path="logging",
            )
        for logger_name, level_val in raw_overrides.items():
            if not isinstance(logger_name, str) or not logger_name:
                raise ConfigValidationError(
                    f"[logging] overrides has invalid logger name "
                    f"{logger_name!r}. Keys must be non-empty strings.",
                    section_path="logging.overrides",
                )
            if (
                not isinstance(level_val, str)
                or level_val.upper() not in VALID_LEVEL_NAMES
            ):
                raise ConfigValidationError(
                    f"[logging] overrides[{logger_name!r}] has invalid level "
                    f"{level_val!r}. Must be one of: "
                    f"{', '.join(sorted(VALID_LEVEL_NAMES))}",
                    section_path=f"logging.overrides.{logger_name}",
                )


# ---------------------------------------------------------------------------
# Adapter section parsing helper
# ---------------------------------------------------------------------------


class _DictConstructible(Protocol):
    """Protocol for runtime config wrappers with a dict factory method."""

    @classmethod
    def from_dict(cls, instance_name: str, data: dict[str, Any]) -> Self: ...


_RTC = TypeVar("_RTC", bound=_DictConstructible)


def _parse_adapter_section(
    data: dict,
    transport: str,
    wrapper_cls: type[_RTC],
    paths: MedrePaths,
) -> dict[str, _RTC]:
    """Parse an ``[adapters.<transport>]`` section.

    Returns a mapping of *instance_name* → runtime config wrapper instance.
    """
    section = data.get(transport, {})
    result: dict[str, _RTC] = {}
    for instance_name, config_table in section.items():
        if not isinstance(config_table, dict):
            continue
        expanded = _expand_paths_in_dict(config_table, paths)
        wrapper = wrapper_cls.from_dict(instance_name, expanded)
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
