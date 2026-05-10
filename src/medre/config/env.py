"""MEDRE_ environment variable override layer.

This module reads ``MEDRE_*`` environment variables and applies them *on top*
of a :class:`~medre.config.model.RuntimeConfig` that was already loaded from
TOML.  The original config is **never mutated**; a new frozen instance is
returned.

Environment variables always win over TOML values.  Adapter-specific env vars
are resolved to the correct adapter instance via target resolution: if
``MEDRE_<TRANSPORT>_ADAPTER_ID`` is set, that adapter is targeted; if only one
adapter of that transport type exists it is targeted automatically; if none
exist a new adapter is created.

Quick reference
---------------
Core:

* ``MEDRE_DB_PATH``   → ``config.storage.path``
* ``MEDRE_LOG_LEVEL`` → ``config.logging.level``

Matrix adapter:

* ``MEDRE_MATRIX_ENABLED``, ``MEDRE_MATRIX_ADAPTER_ID``,
  ``MEDRE_MATRIX_HOMESERVER``, ``MEDRE_MATRIX_USER_ID``,
  ``MEDRE_MATRIX_ACCESS_TOKEN``, ``MEDRE_MATRIX_ROOM_ALLOWLIST``,
  ``MEDRE_MATRIX_DEVICE_ID``, ``MEDRE_MATRIX_STORE_PATH``,
  ``MEDRE_MATRIX_ENCRYPTION_MODE``

Meshtastic adapter:

* ``MEDRE_MESHTASTIC_ENABLED``, ``MEDRE_MESHTASTIC_ADAPTER_ID``,
  ``MEDRE_MESHTASTIC_CONNECTION_TYPE``, ``MEDRE_MESHTASTIC_SERIAL_PORT``,
  ``MEDRE_MESHTASTIC_HOST``, ``MEDRE_MESHTASTIC_PORT``

MeshCore adapter:

* ``MEDRE_MESHCORE_ENABLED``, ``MEDRE_MESHCORE_ADAPTER_ID``,
  ``MEDRE_MESHCORE_CONNECTION_TYPE``, ``MEDRE_MESHCORE_SERIAL_PORT``,
  ``MEDRE_MESHCORE_HOST``, ``MEDRE_MESHCORE_PORT``,
  ``MEDRE_MESHCORE_BLE_ADDRESS``

LXMF adapter:

* ``MEDRE_LXMF_ENABLED``, ``MEDRE_LXMF_ADAPTER_ID``,
  ``MEDRE_LXMF_CONNECTION_TYPE``, ``MEDRE_LXMF_IDENTITY_PATH``,
  ``MEDRE_LXMF_DISPLAY_NAME``, ``MEDRE_LXMF_DESTINATION_HASH``
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field, fields
from typing import Any, Self

from medre.config.errors import ConfigValidationError
from medre.config.model import (
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
)
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.lxmf.config import LxmfConfig

__all__ = ["apply_env_overrides", "MedreEnvConfig"]

# ---------------------------------------------------------------------------
# Env-var name constants
# ---------------------------------------------------------------------------

_SECRET_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_MATRIX_ACCESS_TOKEN",
})

CORE_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_HOME",
    "MEDRE_CONFIG",
    "MEDRE_DB_PATH",
    "MEDRE_LOG_LEVEL",
})

MATRIX_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_MATRIX_ENABLED",
    "MEDRE_MATRIX_ADAPTER_ID",
    "MEDRE_MATRIX_HOMESERVER",
    "MEDRE_MATRIX_USER_ID",
    "MEDRE_MATRIX_ACCESS_TOKEN",
    "MEDRE_MATRIX_ROOM_ALLOWLIST",
    "MEDRE_MATRIX_DEVICE_ID",
    "MEDRE_MATRIX_STORE_PATH",
    "MEDRE_MATRIX_ENCRYPTION_MODE",
})

MESHTASTIC_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_MESHTASTIC_ENABLED",
    "MEDRE_MESHTASTIC_ADAPTER_ID",
    "MEDRE_MESHTASTIC_CONNECTION_TYPE",
    "MEDRE_MESHTASTIC_SERIAL_PORT",
    "MEDRE_MESHTASTIC_HOST",
    "MEDRE_MESHTASTIC_PORT",
})

MESHCORE_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_MESHCORE_ENABLED",
    "MEDRE_MESHCORE_ADAPTER_ID",
    "MEDRE_MESHCORE_CONNECTION_TYPE",
    "MEDRE_MESHCORE_SERIAL_PORT",
    "MEDRE_MESHCORE_HOST",
    "MEDRE_MESHCORE_PORT",
    "MEDRE_MESHCORE_BLE_ADDRESS",
})

LXMF_ENV_NAMES: frozenset[str] = frozenset({
    "MEDRE_LXMF_ENABLED",
    "MEDRE_LXMF_ADAPTER_ID",
    "MEDRE_LXMF_CONNECTION_TYPE",
    "MEDRE_LXMF_IDENTITY_PATH",
    "MEDRE_LXMF_DISPLAY_NAME",
    "MEDRE_LXMF_DESTINATION_HASH",
})

ALL_RECOGNIZED_ENV_NAMES: frozenset[str] = frozenset(
    CORE_ENV_NAMES
    | MATRIX_ENV_NAMES
    | MESHTASTIC_ENV_NAMES
    | MESHCORE_ENV_NAMES
    | LXMF_ENV_NAMES
)

# ---------------------------------------------------------------------------
# Type-coercion helpers
# ---------------------------------------------------------------------------

_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no"})


def _coerce_bool(raw: str, env_name: str) -> bool:
    """Parse a boolean env-var value.

    Accepts ``1/true/yes`` and ``0/false/no`` (case-insensitive).
    Raises :class:`~medre.config.errors.ConfigValidationError` on invalid input.
    """
    normalised = raw.strip().lower()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise ConfigValidationError(
        f"Environment variable {env_name!r} must be a boolean "
        f"(1/true/yes or 0/false/no), got {raw!r}"
    )


def _coerce_int(raw: str, env_name: str) -> int:
    """Parse an integer env-var value.

    Raises :class:`~medre.config.errors.ConfigValidationError` on invalid input.
    """
    try:
        return int(raw.strip())
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"Environment variable {env_name!r} must be an integer, "
            f"got {raw!r}"
        ) from exc


def _coerce_set(raw: str) -> set[str]:
    """Parse a comma-separated env-var value into a ``set[str]``.

    Empty items after splitting are discarded; values are stripped of
    surrounding whitespace.
    """
    return {item.strip() for item in raw.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# EnvProvenance
# ---------------------------------------------------------------------------


class EnvProvenance:
    """Tracks which env vars were actually read and their raw values.

    Used for diagnostics / logging.  Secret values are redacted in repr.
    """

    def __init__(self) -> None:
        self._entries: dict[str, str] = {}

    def record(self, name: str, value: str) -> None:
        """Record that *name* was found in the environment with *value*."""
        self._entries[name] = value

    def redacted_items(self) -> list[tuple[str, str]]:
        """Return recorded entries with secrets redacted."""
        result: list[tuple[str, str]] = []
        for name, value in self._entries.items():
            if name in _SECRET_ENV_NAMES:
                result.append((name, "***REDACTED***"))
            else:
                result.append((name, value))
        return result

    @property
    def set_names(self) -> frozenset[str]:
        """Names of all env vars that were recorded."""
        return frozenset(self._entries.keys())

    def __repr__(self) -> str:
        items = dict(self.redacted_items())
        return f"EnvProvenance({items!r})"

    def __bool__(self) -> bool:
        return bool(self._entries)


# Mapping from env var name → MedreEnvConfig dataclass field name.
_ENV_FIELD_MAP: dict[str, str] = {
    "MEDRE_HOME": "home",
    "MEDRE_CONFIG": "config_path",
    "MEDRE_DB_PATH": "db_path",
    "MEDRE_LOG_LEVEL": "log_level",
    # Matrix
    "MEDRE_MATRIX_ENABLED": "matrix_enabled",
    "MEDRE_MATRIX_ADAPTER_ID": "matrix_adapter_id",
    "MEDRE_MATRIX_HOMESERVER": "matrix_homeserver",
    "MEDRE_MATRIX_USER_ID": "matrix_user_id",
    "MEDRE_MATRIX_ACCESS_TOKEN": "matrix_access_token",
    "MEDRE_MATRIX_ROOM_ALLOWLIST": "matrix_room_allowlist",
    # Internal/test only — not operator-facing.  The adapter discovers
    # device_id via whoami() and derives store_path internally.
    "MEDRE_MATRIX_DEVICE_ID": "matrix_device_id",
    "MEDRE_MATRIX_STORE_PATH": "matrix_store_path",
    "MEDRE_MATRIX_ENCRYPTION_MODE": "matrix_encryption_mode",
    # Meshtastic
    "MEDRE_MESHTASTIC_ENABLED": "meshtastic_enabled",
    "MEDRE_MESHTASTIC_ADAPTER_ID": "meshtastic_adapter_id",
    "MEDRE_MESHTASTIC_CONNECTION_TYPE": "meshtastic_connection_type",
    "MEDRE_MESHTASTIC_SERIAL_PORT": "meshtastic_serial_port",
    "MEDRE_MESHTASTIC_HOST": "meshtastic_host",
    "MEDRE_MESHTASTIC_PORT": "meshtastic_port",
    # MeshCore
    "MEDRE_MESHCORE_ENABLED": "meshcore_enabled",
    "MEDRE_MESHCORE_ADAPTER_ID": "meshcore_adapter_id",
    "MEDRE_MESHCORE_CONNECTION_TYPE": "meshcore_connection_type",
    "MEDRE_MESHCORE_SERIAL_PORT": "meshcore_serial_port",
    "MEDRE_MESHCORE_HOST": "meshcore_host",
    "MEDRE_MESHCORE_PORT": "meshcore_port",
    "MEDRE_MESHCORE_BLE_ADDRESS": "meshcore_ble_address",
    # LXMF
    "MEDRE_LXMF_ENABLED": "lxmf_enabled",
    "MEDRE_LXMF_ADAPTER_ID": "lxmf_adapter_id",
    "MEDRE_LXMF_CONNECTION_TYPE": "lxmf_connection_type",
    "MEDRE_LXMF_IDENTITY_PATH": "lxmf_identity_path",
    "MEDRE_LXMF_DISPLAY_NAME": "lxmf_display_name",
    "MEDRE_LXMF_DESTINATION_HASH": "lxmf_destination_hash",
}

# Adapter-field name sets used by the ``_any_*_set()`` helpers.
_MATRIX_ENV_FIELDS: tuple[str, ...] = (
    "matrix_enabled", "matrix_adapter_id", "matrix_homeserver",
    "matrix_user_id", "matrix_access_token", "matrix_room_allowlist",
    "matrix_device_id", "matrix_store_path", "matrix_encryption_mode",
)
_MESHTASTIC_ENV_FIELDS: tuple[str, ...] = (
    "meshtastic_enabled", "meshtastic_adapter_id",
    "meshtastic_connection_type", "meshtastic_serial_port",
    "meshtastic_host", "meshtastic_port",
)
_MESHCORE_ENV_FIELDS: tuple[str, ...] = (
    "meshcore_enabled", "meshcore_adapter_id",
    "meshcore_connection_type", "meshcore_serial_port",
    "meshcore_host", "meshcore_port", "meshcore_ble_address",
)
_LXMF_ENV_FIELDS: tuple[str, ...] = (
    "lxmf_enabled", "lxmf_adapter_id", "lxmf_connection_type",
    "lxmf_identity_path", "lxmf_display_name", "lxmf_destination_hash",
)

# Adapter-inner-config env field subsets (exclude enabled / adapter_id).
_MATRIX_CONFIG_ENV_FIELDS: tuple[str, ...] = (
    "matrix_homeserver", "matrix_user_id", "matrix_access_token",
    "matrix_room_allowlist", "matrix_device_id", "matrix_store_path",
    "matrix_encryption_mode",
)
_MESHTASTIC_CONFIG_ENV_FIELDS: tuple[str, ...] = (
    "meshtastic_connection_type", "meshtastic_serial_port",
    "meshtastic_host", "meshtastic_port",
)
_MESHCORE_CONFIG_ENV_FIELDS: tuple[str, ...] = (
    "meshcore_connection_type", "meshcore_serial_port",
    "meshcore_host", "meshcore_port", "meshcore_ble_address",
)
_LXMF_CONFIG_ENV_FIELDS: tuple[str, ...] = (
    "lxmf_connection_type", "lxmf_identity_path", "lxmf_display_name",
)


# ---------------------------------------------------------------------------
# MedreEnvConfig
# ---------------------------------------------------------------------------


@dataclass
class MedreEnvConfig:
    """Collected ``MEDRE_*`` environment variables with source tracking.

    All fields are ``None`` when the corresponding env var is not set.  Raw
    string values are stored; type coercion is deferred to
    :func:`apply_env_overrides`.
    """

    provenance: EnvProvenance = field(default_factory=EnvProvenance)

    # -- Core --
    home: str | None = None
    config_path: str | None = None
    db_path: str | None = None
    log_level: str | None = None

    # -- Matrix --
    matrix_enabled: str | None = None
    matrix_adapter_id: str | None = None
    matrix_homeserver: str | None = None
    matrix_user_id: str | None = None
    matrix_access_token: str | None = None
    matrix_room_allowlist: str | None = None
    matrix_device_id: str | None = None
    matrix_store_path: str | None = None
    matrix_encryption_mode: str | None = None

    # -- Meshtastic --
    meshtastic_enabled: str | None = None
    meshtastic_adapter_id: str | None = None
    meshtastic_connection_type: str | None = None
    meshtastic_serial_port: str | None = None
    meshtastic_host: str | None = None
    meshtastic_port: str | None = None

    # -- MeshCore --
    meshcore_enabled: str | None = None
    meshcore_adapter_id: str | None = None
    meshcore_connection_type: str | None = None
    meshcore_serial_port: str | None = None
    meshcore_host: str | None = None
    meshcore_port: str | None = None
    meshcore_ble_address: str | None = None

    # -- LXMF --
    lxmf_enabled: str | None = None
    lxmf_adapter_id: str | None = None
    lxmf_connection_type: str | None = None
    lxmf_identity_path: str | None = None
    lxmf_display_name: str | None = None
    lxmf_destination_hash: str | None = None

    # -- Construction -------------------------------------------------------

    @classmethod
    def from_environ(
        cls,
        environ: dict[str, str] | None = None,
    ) -> Self:
        """Build from *environ* (defaults to ``os.environ``).

        Only recognised ``MEDRE_*`` variable names are captured.
        """
        source = environ if environ is not None else os.environ
        instance = cls()
        provenance = EnvProvenance()

        for env_name, field_name in _ENV_FIELD_MAP.items():
            value = source.get(env_name)
            if value is not None:
                object.__setattr__(instance, field_name, value)
                provenance.record(env_name, value)

        object.__setattr__(instance, "provenance", provenance)
        return instance

    # -- Queries ------------------------------------------------------------

    def has_any_set(self) -> bool:
        """Return ``True`` if any recognised env var is present."""
        return bool(self.provenance)

    def _any_matrix_set(self) -> bool:
        return any(
            getattr(self, f) is not None for f in _MATRIX_ENV_FIELDS
        )

    def _any_meshtastic_set(self) -> bool:
        return any(
            getattr(self, f) is not None for f in _MESHTASTIC_ENV_FIELDS
        )

    def _any_meshcore_set(self) -> bool:
        return any(
            getattr(self, f) is not None for f in _MESHCORE_ENV_FIELDS
        )

    def _any_lxmf_set(self) -> bool:
        return any(
            getattr(self, f) is not None for f in _LXMF_ENV_FIELDS
        )

    # -- Display ------------------------------------------------------------

    def redacted_repr(self) -> str:
        """Return a string representation with secrets redacted."""
        parts: list[str] = []
        for env_name, value in self.provenance.redacted_items():
            field_name = _ENV_FIELD_MAP.get(env_name, env_name)
            parts.append(f"{field_name}={value!r}")
        inner = ", ".join(parts)
        return f"MedreEnvConfig({inner})"

    def to_dict(self) -> dict[str, str]:
        """Return dict of all set env vars (raw values, for diagnostics).

        Secret values are included unredacted — this is intended for
        programmatic use, not for logging.
        """
        return dict(self.provenance._entries)


# ---------------------------------------------------------------------------
# Adapter-specific builders
# ---------------------------------------------------------------------------


def _get_existing_config_kwargs(
    config_cls: type,
    existing_cfg: Any,
) -> dict[str, Any]:
    """Extract all field values from an existing adapter config as a dict."""
    if existing_cfg is None:
        return {}
    return {f.name: getattr(existing_cfg, f.name) for f in fields(config_cls)}


def _build_matrix_config(
    existing: MatrixRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MatrixConfig | None:
    """Build a :class:`MatrixConfig` from existing + env overrides.

    Returns ``None`` if there are no env overrides and no existing config.
    """
    has_env_fields = any(
        getattr(env, f) is not None for f in _MATRIX_CONFIG_ENV_FIELDS
    )

    if not has_env_fields and (existing is None or existing.config is None):
        return None

    kwargs = _get_existing_config_kwargs(
        MatrixConfig,
        existing.config if existing else None,
    )

    # Determine adapter_id
    adapter_id = env.matrix_adapter_id
    if adapter_id is None:
        adapter_id = kwargs.get("adapter_id", target_adapter_id)
    kwargs["adapter_id"] = adapter_id

    # Apply env overrides
    if env.matrix_homeserver is not None:
        kwargs["homeserver"] = env.matrix_homeserver
    if env.matrix_user_id is not None:
        kwargs["user_id"] = env.matrix_user_id
    if env.matrix_access_token is not None:
        kwargs["access_token"] = env.matrix_access_token
    if env.matrix_device_id is not None:
        kwargs["device_id"] = env.matrix_device_id
    if env.matrix_store_path is not None:
        kwargs["store_path"] = env.matrix_store_path
    if env.matrix_room_allowlist is not None:
        kwargs["room_allowlist"] = _coerce_set(env.matrix_room_allowlist)
    if env.matrix_encryption_mode is not None:
        kwargs["encryption_mode"] = env.matrix_encryption_mode

    # Ensure required fields have at least placeholder values
    kwargs.setdefault("homeserver", "")
    kwargs.setdefault("user_id", "")

    return MatrixConfig(**kwargs).validate()


def _build_matrix_runtime(
    existing: MatrixRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MatrixRuntimeConfig:
    """Build a :class:`MatrixRuntimeConfig` from existing + env overrides."""
    adapter_id = (
        env.matrix_adapter_id
        or (existing.adapter_id if existing else target_adapter_id)
    )
    enabled = (
        _coerce_bool(env.matrix_enabled, "MEDRE_MATRIX_ENABLED")
        if env.matrix_enabled is not None
        else (existing.enabled if existing else True)
    )
    config = _build_matrix_config(existing, env, target_adapter_id)
    return MatrixRuntimeConfig(
        adapter_id=adapter_id,
        enabled=enabled,
        config=config,
    )


def _build_meshtastic_config(
    existing: MeshtasticRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MeshtasticConfig | None:
    """Build a :class:`MeshtasticConfig` from existing + env overrides."""
    has_env_fields = any(
        getattr(env, f) is not None for f in _MESHTASTIC_CONFIG_ENV_FIELDS
    )

    if not has_env_fields and (existing is None or existing.config is None):
        return None

    kwargs = _get_existing_config_kwargs(
        MeshtasticConfig,
        existing.config if existing else None,
    )

    adapter_id = env.meshtastic_adapter_id
    if adapter_id is None:
        adapter_id = kwargs.get("adapter_id", target_adapter_id)
    kwargs["adapter_id"] = adapter_id

    if env.meshtastic_connection_type is not None:
        kwargs["connection_type"] = env.meshtastic_connection_type
    if env.meshtastic_serial_port is not None:
        kwargs["serial_port"] = env.meshtastic_serial_port
    if env.meshtastic_host is not None:
        kwargs["host"] = env.meshtastic_host
    if env.meshtastic_port is not None:
        kwargs["port"] = _coerce_int(env.meshtastic_port, "MEDRE_MESHTASTIC_PORT")

    return MeshtasticConfig(**kwargs).validate()


def _build_meshtastic_runtime(
    existing: MeshtasticRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MeshtasticRuntimeConfig:
    """Build a :class:`MeshtasticRuntimeConfig` from existing + env overrides."""
    adapter_id = (
        env.meshtastic_adapter_id
        or (existing.adapter_id if existing else target_adapter_id)
    )
    enabled = (
        _coerce_bool(env.meshtastic_enabled, "MEDRE_MESHTASTIC_ENABLED")
        if env.meshtastic_enabled is not None
        else (existing.enabled if existing else True)
    )
    config = _build_meshtastic_config(existing, env, target_adapter_id)
    return MeshtasticRuntimeConfig(adapter_id=adapter_id, enabled=enabled, config=config)


def _build_meshcore_config(
    existing: MeshCoreRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MeshCoreConfig | None:
    """Build a :class:`MeshCoreConfig` from existing + env overrides."""
    has_env_fields = any(
        getattr(env, f) is not None for f in _MESHCORE_CONFIG_ENV_FIELDS
    )

    if not has_env_fields and (existing is None or existing.config is None):
        return None

    kwargs = _get_existing_config_kwargs(
        MeshCoreConfig,
        existing.config if existing else None,
    )

    adapter_id = env.meshcore_adapter_id
    if adapter_id is None:
        adapter_id = kwargs.get("adapter_id", target_adapter_id)
    kwargs["adapter_id"] = adapter_id

    if env.meshcore_connection_type is not None:
        kwargs["connection_type"] = env.meshcore_connection_type
    if env.meshcore_serial_port is not None:
        kwargs["serial_port"] = env.meshcore_serial_port
    if env.meshcore_host is not None:
        kwargs["host"] = env.meshcore_host
    if env.meshcore_port is not None:
        kwargs["port"] = _coerce_int(env.meshcore_port, "MEDRE_MESHCORE_PORT")
    if env.meshcore_ble_address is not None:
        kwargs["ble_address"] = env.meshcore_ble_address

    return MeshCoreConfig(**kwargs).validate()


def _build_meshcore_runtime(
    existing: MeshCoreRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> MeshCoreRuntimeConfig:
    """Build a :class:`MeshCoreRuntimeConfig` from existing + env overrides."""
    adapter_id = (
        env.meshcore_adapter_id
        or (existing.adapter_id if existing else target_adapter_id)
    )
    enabled = (
        _coerce_bool(env.meshcore_enabled, "MEDRE_MESHCORE_ENABLED")
        if env.meshcore_enabled is not None
        else (existing.enabled if existing else True)
    )
    config = _build_meshcore_config(existing, env, target_adapter_id)
    return MeshCoreRuntimeConfig(adapter_id=adapter_id, enabled=enabled, config=config)


def _build_lxmf_config(
    existing: LxmfRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> LxmfConfig | None:
    """Build a :class:`LxmfConfig` from existing + env overrides."""
    has_env_fields = any(
        getattr(env, f) is not None for f in _LXMF_CONFIG_ENV_FIELDS
    )

    if not has_env_fields and (existing is None or existing.config is None):
        return None

    kwargs = _get_existing_config_kwargs(
        LxmfConfig,
        existing.config if existing else None,
    )

    adapter_id = env.lxmf_adapter_id
    if adapter_id is None:
        adapter_id = kwargs.get("adapter_id", target_adapter_id)
    kwargs["adapter_id"] = adapter_id

    if env.lxmf_connection_type is not None:
        kwargs["connection_type"] = env.lxmf_connection_type
    if env.lxmf_identity_path is not None:
        kwargs["identity_path"] = env.lxmf_identity_path
    if env.lxmf_display_name is not None:
        kwargs["display_name"] = env.lxmf_display_name
    # MEDRE_LXMF_DESTINATION_HASH is recognised but LxmfConfig has no
    # corresponding field yet; silently ignored.

    return LxmfConfig(**kwargs).validate()


def _build_lxmf_runtime(
    existing: LxmfRuntimeConfig | None,
    env: MedreEnvConfig,
    target_adapter_id: str,
) -> LxmfRuntimeConfig:
    """Build a :class:`LxmfRuntimeConfig` from existing + env overrides."""
    adapter_id = (
        env.lxmf_adapter_id
        or (existing.adapter_id if existing else target_adapter_id)
    )
    enabled = (
        _coerce_bool(env.lxmf_enabled, "MEDRE_LXMF_ENABLED")
        if env.lxmf_enabled is not None
        else (existing.enabled if existing else True)
    )
    config = _build_lxmf_config(existing, env, target_adapter_id)
    return LxmfRuntimeConfig(adapter_id=adapter_id, enabled=enabled, config=config)


# ---------------------------------------------------------------------------
# Env-override target resolution
# ---------------------------------------------------------------------------


def _resolve_env_adapter_target(
    transport_name: str,
    adapters_dict: dict[str, Any],
    env: MedreEnvConfig,
    adapter_id_field: str,
) -> tuple[str, Any | None]:
    """Resolve which adapter to override with env vars.

    Returns ``(target_key, existing_config)`` where *target_key* is the
    dict key to write the built runtime config into and *existing_config*
    is the current value at that key (or ``None``).

    Policy
    ------
    1. ``MEDRE_<TRANSPORT>_ADAPTER_ID`` is set → target that adapter.
       If it already exists, override it; otherwise create new.
    2. Exactly one adapter configured → target it regardless of name.
    3. No adapters configured → create a new adapter with the env-supplied
       adapter_id (or ``"default"`` as fallback).
    4. Multiple adapters configured and no ``ADAPTER_ID`` specified →
       raise :class:`ConfigValidationError`.
    """
    env_adapter_id: str | None = getattr(env, adapter_id_field)

    # Case 1: explicit target via env var
    if env_adapter_id is not None:
        existing = adapters_dict.get(env_adapter_id)
        return (env_adapter_id, existing)

    n = len(adapters_dict)

    # Case 2: exactly one adapter — target it
    if n == 1:
        key = next(iter(adapters_dict))
        return (key, adapters_dict[key])

    # Case 3: no adapters — create new
    if n == 0:
        return ("default", None)

    # Case 4: ambiguous — multiple adapters, no target specified
    keys = ", ".join(sorted(adapters_dict.keys()))
    env_var = f"MEDRE_{transport_name.upper()}_ADAPTER_ID"
    raise ConfigValidationError(
        f"Multiple {transport_name} adapters configured ({keys}) but "
        f"{env_var} is not set.  Set it to specify which adapter to "
        f"override with environment variables."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_env_overrides(
    config: RuntimeConfig,
    paths: Any | None = None,
) -> RuntimeConfig:
    """Apply ``MEDRE_*`` env vars on top of a parsed :class:`RuntimeConfig`.

    Returns a **new** :class:`RuntimeConfig` with overridden values.  The
    original *config* is not mutated.

    Parameters
    ----------
    config:
        The base configuration (typically loaded from TOML).
    paths:
        Reserved for future use (e.g. resolving relative store paths).
        Currently unused.

    Returns
    -------
    RuntimeConfig
        A new frozen config instance with env-var overrides applied.
    """
    env = MedreEnvConfig.from_environ()

    if not env.has_any_set():
        return config

    # ------------------------------------------------------------------
    # Core overrides
    # ------------------------------------------------------------------
    new_logging = config.logging
    if env.log_level is not None:
        new_logging = dataclasses.replace(config.logging, level=env.log_level)

    new_storage = config.storage
    if env.db_path is not None:
        new_storage = dataclasses.replace(config.storage, path=env.db_path)

    # ------------------------------------------------------------------
    # Adapter overrides — use target resolution to find the right adapter
    # ------------------------------------------------------------------
    matrix_dict = dict(config.adapters.matrix)
    meshtastic_dict = dict(config.adapters.meshtastic)
    meshcore_dict = dict(config.adapters.meshcore)
    lxmf_dict = dict(config.adapters.lxmf)

    if env._any_matrix_set():
        target_key, existing = _resolve_env_adapter_target(
            "matrix", matrix_dict, env, "matrix_adapter_id",
        )
        matrix_dict[target_key] = _build_matrix_runtime(
            existing, env, target_key,
        )

    if env._any_meshtastic_set():
        target_key, existing = _resolve_env_adapter_target(
            "meshtastic", meshtastic_dict, env, "meshtastic_adapter_id",
        )
        meshtastic_dict[target_key] = _build_meshtastic_runtime(
            existing, env, target_key,
        )

    if env._any_meshcore_set():
        target_key, existing = _resolve_env_adapter_target(
            "meshcore", meshcore_dict, env, "meshcore_adapter_id",
        )
        meshcore_dict[target_key] = _build_meshcore_runtime(
            existing, env, target_key,
        )

    if env._any_lxmf_set():
        target_key, existing = _resolve_env_adapter_target(
            "lxmf", lxmf_dict, env, "lxmf_adapter_id",
        )
        lxmf_dict[target_key] = _build_lxmf_runtime(
            existing, env, target_key,
        )

    new_adapters = dataclasses.replace(
        config.adapters,
        matrix=matrix_dict,
        meshtastic=meshtastic_dict,
        meshcore=meshcore_dict,
        lxmf=lxmf_dict,
    )

    return dataclasses.replace(
        config,
        logging=new_logging,
        storage=new_storage,
        adapters=new_adapters,
    )
