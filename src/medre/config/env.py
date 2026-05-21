"""MEDRE_ environment variable override layer.

This module reads ``MEDRE_*`` environment variables and applies them *on top*
of a :class:`~medre.config.model.RuntimeConfig` that was already loaded from
TOML.  The original config is **never mutated**; a new frozen instance is
returned.

Environment variables always win over TOML values.  Adapter overrides use
instance-scoped env vars of the form::

    MEDRE_ADAPTER__<TOKEN>__<FIELD>=<value>

where ``<TOKEN>`` is the uppercased, non-alphanumeric-stripped form of the
adapter's ``adapter_id`` (see :func:`normalize_adapter_id`) and ``<FIELD>`` is
a field name on the adapter's config dataclass (or ``enabled``).

Quick reference
---------------
Core:

* ``MEDRE_DB_PATH``   → ``config.storage.path``
* ``MEDRE_LOG_LEVEL`` → ``config.logging.level``
* ``MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES``, etc.

Adapter overrides (instance-scoped):

* ``MEDRE_ADAPTER__MATRIX_PRIMARY__HOMESERVER`` → overrides ``homeserver``
  on the adapter whose ``adapter_id`` normalizes to ``MATRIX_PRIMARY``
* ``MEDRE_ADAPTER__MESHTASTIC_RADIO_A__HOST`` → overrides ``host`` on the
  adapter whose ``adapter_id`` normalizes to ``MESHTASTIC_RADIO_A``
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, Self, get_args, get_type_hints

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.errors import ConfigValidationError
from medre.config.model import (
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
)

__all__ = [
    "apply_env_overrides",
    "apply_instance_env_overrides",
    "detect_token_collisions",
    "MedreEnvConfig",
    "normalize_adapter_id",
]

# ---------------------------------------------------------------------------
# Env-var name constants (core only)
# ---------------------------------------------------------------------------

CORE_ENV_NAMES: frozenset[str] = frozenset(
    {
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "MEDRE_DB_PATH",
        "MEDRE_LOG_LEVEL",
        "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES",
        "MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS",
        "MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
        "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS",
    }
)

_ADAPTER_ENV_PREFIX = "MEDRE_ADAPTER__"

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
            f"Environment variable {env_name!r} must be an integer, " f"got {raw!r}"
        ) from exc


def _coerce_float(raw: str, env_name: str) -> float:
    """Parse a float env-var value.

    Raises :class:`~medre.config.errors.ConfigValidationError` on invalid input.
    """
    try:
        return float(raw.strip())
    except (ValueError, TypeError) as exc:
        raise ConfigValidationError(
            f"Environment variable {env_name!r} must be a number, " f"got {raw!r}"
        ) from exc


def _coerce_set(raw: str) -> set[str]:
    """Parse a comma-separated env-var value into a ``set[str]``.

    Empty items after splitting are discarded; values are stripped of
    surrounding whitespace.
    """
    return {item.strip() for item in raw.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# Heuristic secret detection
# ---------------------------------------------------------------------------

_SECRET_FIELD_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL", re.IGNORECASE
)


def _is_secret_field(field_name: str) -> bool:
    """Return ``True`` if *field_name* looks like a secret field."""
    return bool(_SECRET_FIELD_RE.search(field_name))


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
        """Return recorded entries with secrets redacted.

        Secret detection is heuristic: any env var whose final segment
        (after the last ``__`` for adapter vars, or whose name for core
        vars) matches ``TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL`` is
        redacted.
        """
        result: list[tuple[str, str]] = []
        for name, value in self._entries.items():
            # For MEDRE_ADAPTER__TOKEN__FIELD, extract FIELD.
            if name.startswith(_ADAPTER_ENV_PREFIX):
                parts = name.split("__", 2)
                field_part = parts[2] if len(parts) >= 3 else ""
                if _is_secret_field(field_part):
                    result.append((name, "***REDACTED***"))
                    continue
            if _is_secret_field(name):
                result.append((name, "***REDACTED***"))
                continue
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


# Mapping from core env var name → MedreEnvConfig dataclass field name.
_ENV_FIELD_MAP: dict[str, str] = {
    "MEDRE_HOME": "home",
    "MEDRE_CONFIG": "config_path",
    "MEDRE_DB_PATH": "db_path",
    "MEDRE_LOG_LEVEL": "log_level",
    # Runtime limits
    "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES": "max_inflight_deliveries",
    "MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS": "max_inflight_replay_events",
    "MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS": "shutdown_drain_timeout_seconds",
    "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS": "delivery_acquire_timeout_seconds",
}

# ---------------------------------------------------------------------------
# Adapter-token normalization
# ---------------------------------------------------------------------------


def normalize_adapter_id(adapter_id: str) -> str:
    """Convert an adapter_id to an env token.

    Non-alphanumeric characters are replaced with ``_``, consecutive
    underscores are collapsed, leading/trailing underscores are stripped,
    and the result is uppercased.

    Examples::

        matrix-primary → MATRIX_PRIMARY
        matrix_primary → MATRIX_PRIMARY
        radio.a        → RADIO_A
        meshcore/tbeam → MESHCORE_TBEAM
    """
    token = re.sub(r"[^a-zA-Z0-9]", "_", adapter_id)
    token = re.sub(r"_+", "_", token)
    token = token.strip("_")
    return token.upper()


def detect_token_collisions(adapters_dict: dict[str, Any]) -> None:
    """Raise :class:`ConfigValidationError` if two adapter IDs normalize to
    the same token.

    Parameters
    ----------
    adapters_dict:
        Mapping of adapter_id → config (any value; only keys are inspected).
    """
    tokens: dict[str, list[str]] = {}
    for adapter_id in adapters_dict:
        token = normalize_adapter_id(adapter_id)
        tokens.setdefault(token, []).append(adapter_id)
    collisions = {t: ids for t, ids in tokens.items() if len(ids) > 1}
    if collisions:
        msgs: list[str] = []
        for _tok, ids in collisions.items():
            msgs.append(
                f"Adapter IDs {ids} both normalize to the same token "
                f"— rename one adapter_id"
            )
        raise ConfigValidationError("; ".join(msgs))


# ---------------------------------------------------------------------------
# Transport config-class registry
# ---------------------------------------------------------------------------

# Maps transport name → (ConfigClass, RuntimeConfigClass).
_TRANSPORT_REGISTRY: dict[str, tuple[type, type]] = {
    "matrix": (MatrixConfig, MatrixRuntimeConfig),
    "meshtastic": (MeshtasticConfig, MeshtasticRuntimeConfig),
    "meshcore": (MeshCoreConfig, MeshCoreRuntimeConfig),
    "lxmf": (LxmfConfig, LxmfRuntimeConfig),
}


def _valid_fields_for_transport(transport: str) -> frozenset[str]:
    """Return the set of valid field names for *transport*.

    Includes ``"enabled"`` (from the runtime wrapper) and all fields from
    the transport's config dataclass.
    """
    config_cls = _TRANSPORT_REGISTRY[transport][0]
    config_fields = frozenset(f.name for f in fields(config_cls))
    return config_fields | frozenset({"enabled"})


def _get_field_type(transport: str, field_name: str) -> type | None:
    """Return the Python type annotation for a field, or ``None``."""
    if field_name == "enabled":
        return bool
    config_cls = _TRANSPORT_REGISTRY[transport][0]
    hints = get_type_hints(config_cls)
    hint = hints.get(field_name)
    if hint is None:
        return None
    # Unwrap Optional / X | None to get the concrete type.
    origin = getattr(hint, "__origin__", None)
    if origin is not None:
        # For Union types, pick the first non-None arg.
        args = get_args(hint)
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            return non_none[0]
    return hint


def _is_set_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* looks like ``set[...]``."""
    if hint is None:
        return False
    origin = getattr(hint, "__origin__", None)
    if origin is set or origin is frozenset:
        return True
    args = getattr(hint, "__args__", None)
    if args is not None:
        return any(_is_set_type(a) for a in args)
    return False


def _is_int_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``int`` (unwrapped)."""
    return hint is int


def _is_float_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``float`` (unwrapped)."""
    return hint is float


def _is_bool_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``bool`` (unwrapped)."""
    return hint is bool


def _coerce_field_value(
    raw: str,
    field_name: str,
    field_type: type | None,
    env_var_name: str,
) -> Any:
    """Coerce a raw env-var string to the expected Python type."""
    if _is_bool_type(field_type):
        return _coerce_bool(raw, env_var_name)
    if _is_int_type(field_type):
        return _coerce_int(raw, env_var_name)
    if _is_float_type(field_type):
        return _coerce_float(raw, env_var_name)
    if _is_set_type(field_type):
        return _coerce_set(raw)
    # Default: treat as string.
    return raw


# ---------------------------------------------------------------------------
# Adapter env-var parsing
# ---------------------------------------------------------------------------


def _parse_adapter_env_vars(
    environ: dict[str, str] | os._Environ[str],  # type: ignore[attr-defined]
) -> dict[str, dict[str, str]]:
    """Parse ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` vars from *environ*.

    Returns a nested dict: ``{token: {field: raw_value}}``.
    """
    result: dict[str, dict[str, str]] = {}
    prefix = _ADAPTER_ENV_PREFIX
    for name, value in environ.items():
        if not name.startswith(prefix):
            continue
        remainder = name[len(prefix):]
        # Split on double-underscore: TOKEN__FIELD
        parts = remainder.split("__", 1)
        if len(parts) != 2:
            continue
        token, field_name = parts
        if not token or not field_name:
            continue
        result.setdefault(token, {})[field_name] = value
    return result


# ---------------------------------------------------------------------------
# MedreEnvConfig
# ---------------------------------------------------------------------------


@dataclass
class MedreEnvConfig:
    """Collected ``MEDRE_*`` environment variables with source tracking.

    Core fields are ``None`` when the corresponding env var is not set.
    Adapter-instance overrides are stored in ``instance_overrides`` as
    ``{token: {field: raw_value}}``.
    """

    provenance: EnvProvenance = field(default_factory=EnvProvenance)

    # -- Core --
    home: str | None = None
    config_path: str | None = None
    db_path: str | None = None
    log_level: str | None = None

    # -- Runtime limits --
    max_inflight_deliveries: str | None = None
    max_inflight_replay_events: str | None = None
    shutdown_drain_timeout_seconds: str | None = None
    delivery_acquire_timeout_seconds: str | None = None

    # -- Instance-scoped adapter overrides --
    instance_overrides: dict[str, dict[str, str]] = field(default_factory=dict)

    # -- Construction -------------------------------------------------------

    @classmethod
    def from_environ(
        cls,
        environ: dict[str, str] | None = None,
    ) -> Self:
        """Build from *environ* (defaults to ``os.environ``).

        Core ``MEDRE_*`` variables are captured into typed fields.
        ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` variables are parsed into
        ``instance_overrides``.
        """
        source = environ if environ is not None else os.environ
        instance = cls()
        provenance = EnvProvenance()

        # Core env vars.
        for env_name, field_name in _ENV_FIELD_MAP.items():
            value = source.get(env_name)
            if value is not None:
                object.__setattr__(instance, field_name, value)
                provenance.record(env_name, value)

        # Instance-scoped adapter overrides.
        adapter_overrides = _parse_adapter_env_vars(source)
        if adapter_overrides:
            object.__setattr__(instance, "instance_overrides", adapter_overrides)
            for token, field_map in adapter_overrides.items():
                for field_name, value in field_map.items():
                    env_var = f"{_ADAPTER_ENV_PREFIX}{token}__{field_name}"
                    provenance.record(env_var, value)

        object.__setattr__(instance, "provenance", provenance)
        return instance

    # -- Queries ------------------------------------------------------------

    def has_any_set(self) -> bool:
        """Return ``True`` if any recognised env var is present."""
        return bool(self.provenance)

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
# Instance-scoped env override application
# ---------------------------------------------------------------------------


def apply_instance_env_overrides(
    config: RuntimeConfig,
    instance_overrides: dict[str, dict[str, str]],
) -> RuntimeConfig:
    """Apply ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` overrides to *config*.

    Parameters
    ----------
    config:
        The base configuration.
    instance_overrides:
        Parsed adapter env vars as ``{token: {field: raw_value}}``.

    Returns
    -------
    RuntimeConfig
        A new frozen config with adapter overrides applied.

    Raises
    ------
    ConfigValidationError
        If a token doesn't match any adapter, a field is unsupported,
        or type coercion fails.
    """
    if not instance_overrides:
        return config

    # Build global mapping: token → (transport, adapter_key).
    # adapter_key is the dict key in the transport's adapter dict (usually
    # matches adapter_id unless the TOML instance name differs).
    token_to_adapter: dict[str, tuple[str, str]] = {}
    for transport, group in (
        ("matrix", config.adapters.matrix),
        ("meshtastic", config.adapters.meshtastic),
        ("meshcore", config.adapters.meshcore),
        ("lxmf", config.adapters.lxmf),
    ):
        for adapter_key, rtc in group.items():
            token = normalize_adapter_id(rtc.adapter_id)
            token_to_adapter[token] = (transport, adapter_key)

    # Check for collisions in the global token space.
    # Rebuild a dict suitable for detect_token_collisions.
    all_adapters_by_id: dict[str, Any] = {}
    for transport, group in (
        ("matrix", config.adapters.matrix),
        ("meshtastic", config.adapters.meshtastic),
        ("meshcore", config.adapters.meshcore),
        ("lxmf", config.adapters.lxmf),
    ):
        for _key, rtc in group.items():
            all_adapters_by_id[rtc.adapter_id] = rtc
    detect_token_collisions(all_adapters_by_id)

    # Collect overrides by transport for batch application.
    transport_overrides: dict[str, dict[str, dict[str, str]]] = {}
    unknown_tokens: list[str] = []

    for token, field_map in instance_overrides.items():
        match = token_to_adapter.get(token)
        if match is None:
            unknown_tokens.append(token)
            continue
        transport, adapter_key = match
        transport_overrides.setdefault(transport, {})[adapter_key] = field_map

    if unknown_tokens:
        known = sorted(token_to_adapter.keys())
        raise ConfigValidationError(
            f"Unknown adapter tokens in env vars: {unknown_tokens}. "
            f"Known tokens: {known}"
        )

    # Apply overrides per transport.
    new_matrix = dict(config.adapters.matrix)
    new_meshtastic = dict(config.adapters.meshtastic)
    new_meshcore = dict(config.adapters.meshcore)
    new_lxmf = dict(config.adapters.lxmf)
    transport_dicts: dict[str, dict[str, Any]] = {
        "matrix": new_matrix,
        "meshtastic": new_meshtastic,
        "meshcore": new_meshcore,
        "lxmf": new_lxmf,
    }

    for transport, adapter_overrides in transport_overrides.items():
        valid_fields = _valid_fields_for_transport(transport)
        config_cls, runtime_cls = _TRANSPORT_REGISTRY[transport]
        transport_dict = transport_dicts[transport]

        for adapter_key, field_map in adapter_overrides.items():
            # Validate field names.
            unsupported = set(field_map.keys()) - valid_fields
            if unsupported:
                raise ConfigValidationError(
                    f"Unsupported fields for {transport} adapter "
                    f"{adapter_key!r}: {sorted(unsupported)}. "
                    f"Valid fields: {sorted(valid_fields)}"
                )

            existing = transport_dict.get(adapter_key)
            # Build new runtime config from existing.
            if existing is not None:
                new_enabled = existing.enabled
                new_config = existing.config
                new_adapter_id = existing.adapter_id
                new_adapter_kind = existing.adapter_kind
            else:
                new_enabled = True
                new_config = None
                new_adapter_id = adapter_key
                new_adapter_kind = "real"

            # Separate enabled/adapter_id overrides from config-field overrides
            # so we can build config_kwargs once.
            for field_name, raw_value in field_map.items():
                env_var = (
                    f"{_ADAPTER_ENV_PREFIX}"
                    f"{normalize_adapter_id(new_adapter_id)}__{field_name}"
                )

                if field_name == "enabled":
                    new_enabled = _coerce_bool(raw_value, env_var)
                elif field_name == "adapter_id":
                    new_adapter_id = raw_value

            # Apply config-field overrides (fields other than enabled/adapter_id).
            config_field_names = {
                k for k in field_map if k not in ("enabled", "adapter_id")
            }
            if config_field_names:
                # Start from existing config values or fresh defaults.
                if new_config is not None:
                    config_kwargs = {
                        f.name: getattr(new_config, f.name)
                        for f in fields(config_cls)
                    }
                else:
                    config_kwargs: dict[str, Any] = {"adapter_id": new_adapter_id}
                    for f in fields(config_cls):
                        if f.name == "adapter_id":
                            continue
                        if f.default is not dataclasses.MISSING:
                            config_kwargs[f.name] = f.default
                        elif f.default_factory is not dataclasses.MISSING:
                            config_kwargs[f.name] = f.default_factory()

                config_kwargs["adapter_id"] = new_adapter_id
                for field_name in config_field_names:
                    raw_value = field_map[field_name]
                    env_var = (
                        f"{_ADAPTER_ENV_PREFIX}"
                        f"{normalize_adapter_id(new_adapter_id)}__{field_name}"
                    )
                    field_type = _get_field_type(transport, field_name)
                    coerced = _coerce_field_value(
                        raw_value, field_name, field_type, env_var
                    )
                    config_kwargs[field_name] = coerced
                new_config = config_cls(**config_kwargs).validate()

            new_rtc = runtime_cls(
                adapter_id=new_adapter_id,
                enabled=new_enabled,
                adapter_kind=new_adapter_kind,
                config=new_config,
            )
            transport_dict[adapter_key] = new_rtc

    from medre.config.model import AdapterConfigSet

    new_adapters = AdapterConfigSet(
        matrix=new_matrix,
        meshtastic=new_meshtastic,
        meshcore=new_meshcore,
        lxmf=new_lxmf,
    )

    return dataclasses.replace(config, adapters=new_adapters)


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
    # Runtime limits overrides
    # ------------------------------------------------------------------
    new_limits = config.limits
    has_limits_override = any(
        getattr(env, f) is not None
        for f in (
            "max_inflight_deliveries",
            "max_inflight_replay_events",
            "shutdown_drain_timeout_seconds",
            "delivery_acquire_timeout_seconds",
        )
    )
    if has_limits_override:
        limits_kwargs: dict[str, Any] = {}
        if env.max_inflight_deliveries is not None:
            limits_kwargs["max_inflight_deliveries"] = _coerce_int(
                env.max_inflight_deliveries,
                "MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES",
            )
        if env.max_inflight_replay_events is not None:
            limits_kwargs["max_inflight_replay_events"] = _coerce_int(
                env.max_inflight_replay_events,
                "MEDRE_RUNTIME_MAX_INFLIGHT_REPLAY_EVENTS",
            )
        if env.shutdown_drain_timeout_seconds is not None:
            limits_kwargs["shutdown_drain_timeout_seconds"] = _coerce_int(
                env.shutdown_drain_timeout_seconds,
                "MEDRE_RUNTIME_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
            )
        if env.delivery_acquire_timeout_seconds is not None:
            limits_kwargs["delivery_acquire_timeout_seconds"] = _coerce_float(
                env.delivery_acquire_timeout_seconds,
                "MEDRE_RUNTIME_DELIVERY_ACQUIRE_TIMEOUT_SECONDS",
            )
        new_limits = dataclasses.replace(config.limits, **limits_kwargs).validate()

    # ------------------------------------------------------------------
    # Instance-scoped adapter overrides
    # ------------------------------------------------------------------
    config = dataclasses.replace(
        config,
        logging=new_logging,
        storage=new_storage,
        limits=new_limits,
    )

    config = apply_instance_env_overrides(config, env.instance_overrides)

    return config
