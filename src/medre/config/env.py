"""MEDRE_ environment variable override layer.

This module reads ``MEDRE_*`` environment variables and applies them *on top*
of a :class:`~medre.config.model.RuntimeConfig` that was already loaded from
TOML.  The original config is **never mutated**; a new frozen instance is
returned.

Environment variables always win over TOML values.  Adapter overrides use
instance-scoped env vars of the form::

    MEDRE_ADAPTER__<TOKEN>__<FIELD>=<value>

where ``<TOKEN>`` is the uppercased form of the adapter's ``adapter_id``
with non-alphanumeric characters replaced with underscores
(see :func:`normalize_adapter_id`) and ``<FIELD>`` is
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
from typing import Any, Self, get_args, get_origin, get_type_hints

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
    "ParsedAdapterEnvValue",
    "ProvenanceEntry",
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

ALL_RECOGNIZED_ENV_NAMES: frozenset[str] = CORE_ENV_NAMES

_ADAPTER_ENV_PREFIX = "MEDRE_ADAPTER__"

_REJECTED_LEGACY_PREFIXES: tuple[str, ...] = (
    "MEDRE_MATRIX_",
    "MEDRE_MESHTASTIC_",
    "MEDRE_MESHCORE_",
    "MEDRE_LXMF_",
)

# ---------------------------------------------------------------------------
# Type-coercion helpers
# ---------------------------------------------------------------------------

_TRUE_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})
_FALSE_VALUES: frozenset[str] = frozenset({"0", "false", "no"})


def _unwrap_optional_type(hint: Any) -> Any:
    """Unwrap ``Optional[X]`` / ``X | None`` to the concrete type ``X``.

    Handles both ``typing.Optional[X]`` and PEP-604 ``X | None`` syntax.
    Returns *hint* unchanged if it is not an Optional wrapper.
    """
    args = get_args(hint)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return non_none[0]
    return hint


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
    r"TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL|BLE|IDENTITY",
    re.IGNORECASE,
)


def _is_secret_field(field_name: str) -> bool:
    """Return ``True`` if *field_name* looks like a secret field."""
    return bool(_SECRET_FIELD_RE.search(field_name))


# ---------------------------------------------------------------------------
# EnvProvenance
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceEntry:
    """Structured metadata for a single env-var override."""

    env_var_name: str
    source_kind: str  # "core" or "instance"
    raw_value: str
    target_adapter_token: str | None = None
    target_transport: str | None = None
    target_field: str | None = None


class EnvProvenance:
    """Tracks which env vars were actually read and their raw values.

    Used for diagnostics / logging.  Secret values are redacted in repr.
    Each entry carries structured metadata (source kind, target adapter,
    transport, and field).
    """

    def __init__(self) -> None:
        self._entries: dict[str, ProvenanceEntry] = {}

    def record(
        self,
        name: str,
        value: str,
        *,
        source_kind: str = "core",
        target_adapter_token: str | None = None,
        target_transport: str | None = None,
        target_field: str | None = None,
    ) -> None:
        """Record that *name* was found in the environment with *value*."""
        self._entries[name] = ProvenanceEntry(
            env_var_name=name,
            source_kind=source_kind,
            raw_value=value,
            target_adapter_token=target_adapter_token,
            target_transport=target_transport,
            target_field=target_field,
        )

    def redacted_items(self) -> list[tuple[str, str]]:
        """Return recorded entries with secrets redacted.

        Secret detection is heuristic: any env var whose final segment
        (after the last ``__`` for adapter vars, or whose name for core
        vars) matches ``TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL|BLE|IDENTITY``
        is redacted.
        """
        result: list[tuple[str, str]] = []
        for entry in self._entries.values():
            name = entry.env_var_name
            value = entry.raw_value
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

    @property
    def entries(self) -> list[ProvenanceEntry]:
        """All recorded provenance entries in insertion order."""
        return list(self._entries.values())

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
                f"Adapter IDs {ids} both normalize to {_tok}; "
                f"rename one adapter_id."
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
    return _unwrap_optional_type(hint)


def _is_set_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* looks like ``set[...]``."""
    if hint is None:
        return False
    unwrapped = _unwrap_optional_type(hint)
    origin = get_origin(unwrapped)
    if origin is set or origin is frozenset:
        return True
    args = get_args(unwrapped)
    if args:
        return any(_is_set_type(a) for a in args)
    return False


def _is_int_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``int`` (unwrapped)."""
    return _unwrap_optional_type(hint) is int


def _is_float_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``float`` (unwrapped)."""
    return _unwrap_optional_type(hint) is float


def _is_bool_type(hint: type | None) -> bool:
    """Return ``True`` if *hint* is ``bool`` (unwrapped)."""
    return _unwrap_optional_type(hint) is bool


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


@dataclass
class ParsedAdapterEnvValue:
    """A single parsed ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` value."""

    env_var_name: str
    raw_value: str


def _parse_adapter_env_vars(
    environ: dict[str, str] | os._Environ[str],  # type: ignore[attr-defined]
) -> dict[str, dict[str, ParsedAdapterEnvValue]]:
    """Parse ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` vars from *environ*.

    Returns a nested dict: ``{token: {field: ParsedAdapterEnvValue}}``.

    Raises :class:`~medre.config.errors.ConfigValidationError` if any
    ``MEDRE_ADAPTER__`` variable has a malformed shape (wrong number of
    segments, empty token, or empty field) or if duplicate normalized
    fields are found (same token+field from different env var names).
    """
    result: dict[str, dict[str, ParsedAdapterEnvValue]] = {}
    prefix = _ADAPTER_ENV_PREFIX
    malformed: list[str] = []
    duplicates: list[str] = []

    for name, value in environ.items():
        if not name.startswith(prefix):
            continue
        remainder = name[len(prefix):]
        if not remainder:
            malformed.append(name)
            continue
        parts = remainder.split("__")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            malformed.append(name)
            continue
        token = parts[0].upper()
        field_name = parts[1].lower()
        parsed = ParsedAdapterEnvValue(env_var_name=name, raw_value=value)
        field_map = result.setdefault(token, {})
        if field_name in field_map:
            duplicates.append(
                f"{field_map[field_name].env_var_name} and {name} both "
                f"normalize to token={token!r} field={field_name!r}"
            )
        field_map[field_name] = parsed

    if malformed:
        raise ConfigValidationError(
            f"Malformed MEDRE_ADAPTER__ environment variable(s): "
            f"{sorted(malformed)}. Expected shape: "
            f"MEDRE_ADAPTER__<TOKEN>__<FIELD> with non-empty TOKEN and FIELD."
        )
    if duplicates:
        raise ConfigValidationError(
            f"Duplicate normalized adapter fields: {'; '.join(duplicates)}. "
            f"Each token/field combination must come from exactly one env var."
        )
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
    instance_overrides: dict[str, dict[str, ParsedAdapterEnvValue]] = field(default_factory=dict)

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

        # Reject legacy transport-specific env var prefixes.
        legacy_found: list[str] = []
        for key in source:
            if key.startswith(_ADAPTER_ENV_PREFIX):
                continue
            for prefix in _REJECTED_LEGACY_PREFIXES:
                if key.startswith(prefix):
                    legacy_found.append(key)
                    break
        if legacy_found:
            raise ConfigValidationError(
                f"Legacy transport env variable(s) detected: "
                f"{sorted(legacy_found)}. "
                f"These are no longer supported. Use instance-scoped "
                f"MEDRE_ADAPTER__<TOKEN>__<FIELD> variables instead."
            )

        # Core env vars.
        for env_name, field_name in _ENV_FIELD_MAP.items():
            value = source.get(env_name)
            if value is not None:
                object.__setattr__(instance, field_name, value)
                provenance.record(env_name, value, source_kind="core")

        # Instance-scoped adapter overrides.
        adapter_overrides = _parse_adapter_env_vars(source)
        if adapter_overrides:
            object.__setattr__(instance, "instance_overrides", adapter_overrides)
            for token, field_map in adapter_overrides.items():
                for field_name, parsed in field_map.items():
                    provenance.record(
                        parsed.env_var_name,
                        parsed.raw_value,
                        source_kind="instance",
                        target_adapter_token=token,
                        target_field=field_name,
                    )

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
        return {e.env_var_name: e.raw_value for e in self.provenance.entries}


# ---------------------------------------------------------------------------
# Instance-scoped env override application
# ---------------------------------------------------------------------------


def _iter_configured_adapters(
    config: RuntimeConfig,
) -> list[tuple[str, str, str, Any]]:
    """Return ``(transport, key, adapter_id, runtime_config)`` for all adapters."""
    result: list[tuple[str, str, str, Any]] = []
    for transport, group in (
        ("matrix", config.adapters.matrix),
        ("meshtastic", config.adapters.meshtastic),
        ("meshcore", config.adapters.meshcore),
        ("lxmf", config.adapters.lxmf),
    ):
        for key, rtc in group.items():
            result.append((transport, key, rtc.adapter_id, rtc))
    return result


def _collect_configured_adapter_refs(config: RuntimeConfig) -> list[tuple[str, str, str]]:
    """Return list of (transport, adapter_key, adapter_id) for all configured adapters."""
    refs: list[tuple[str, str, str]] = []
    for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
        group = getattr(config.adapters, transport, {})
        for key, rtc in group.items():
            refs.append((transport, key, rtc.adapter_id))
    return refs


def _check_token_collisions(refs: list[tuple[str, str, str]]) -> str | None:
    """Check for duplicate normalized tokens. Returns error message or None."""
    token_groups: dict[str, list[tuple[str, str, str]]] = {}
    for transport, key, adapter_id in refs:
        token = normalize_adapter_id(adapter_id)
        token_groups.setdefault(token, []).append((transport, key, adapter_id))
    for token, adapters in token_groups.items():
        if len(adapters) > 1:
            details = "; ".join(
                f"{t}.{k} adapter_id={a!r}" for t, k, a in adapters
            )
            return (
                f"Adapter env token collision for {token}: {details}. "
                f"Rename one adapter_id."
            )
    return None


def apply_instance_env_overrides(
    config: RuntimeConfig,
    instance_overrides: dict[str, dict[str, ParsedAdapterEnvValue]],
    provenance: EnvProvenance | None = None,
) -> RuntimeConfig:
    """Apply ``MEDRE_ADAPTER__<TOKEN>__<FIELD>`` overrides to *config*.

    For tokens that match an existing TOML adapter, field values are
    overridden in-place.  For tokens with **no** matching adapter but
    with a ``TRANSPORT`` field, a brand-new adapter instance is created
    from environment variables alone (env-first creation).

    Parameters
    ----------
    config:
        The base configuration.
    instance_overrides:
        Parsed adapter env vars as ``{token: {field: ParsedAdapterEnvValue}}``.
    provenance:
        Optional provenance tracker.  When provided, *target_transport*
        is back-filled on provenance entries for newly created adapters.

    Returns
    -------
    RuntimeConfig
        A new frozen config with adapter overrides applied.

    Raises
    ------
    ConfigValidationError
        If a token doesn't match any adapter and has no ``TRANSPORT``,
        a field is unsupported, type coercion fails, two adapters
        normalize to the same token, or adapter IDs collide after
        creation.
    """
    refs = _collect_configured_adapter_refs(config)
    collision_err = _check_token_collisions(refs)
    if collision_err:
        raise ConfigValidationError(collision_err)

    if not instance_overrides:
        return config

    # Build global mapping: token → (transport, adapter_key).
    token_to_adapter: dict[str, tuple[str, str]] = {}
    for transport, adapter_key, adapter_id, _rtc in _iter_configured_adapters(config):
        token = normalize_adapter_id(adapter_id)
        token_to_adapter[token] = (transport, adapter_key)

    # Separate tokens into matched (existing adapter) and new (has TRANSPORT).
    transport_overrides: dict[str, dict[str, dict[str, ParsedAdapterEnvValue]]] = {}
    created_tokens: dict[str, dict[str, ParsedAdapterEnvValue]] = {}
    unknown_tokens: list[str] = []

    for token, field_map in instance_overrides.items():
        match = token_to_adapter.get(token)
        if match is not None:
            transport, adapter_key = match
            transport_overrides.setdefault(transport, {})[adapter_key] = field_map
        elif "transport" in field_map:
            created_tokens[token] = field_map
        else:
            unknown_tokens.append(token)

    if unknown_tokens:
        known = sorted(token_to_adapter.keys())
        msgs: list[str] = []
        for token in unknown_tokens:
            msgs.append(
                f"Unknown adapter token {token!r}: no adapter found in config "
                f"and no TRANSPORT specified. Set "
                f"MEDRE_ADAPTER__{token}__TRANSPORT=<transport> to create one, "
                f"or ensure a TOML adapter with a matching adapter_id exists. "
                f"Known tokens: {known}"
            )
        raise ConfigValidationError("; ".join(msgs))

    # Prepare mutable transport dicts.
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

    # -- Apply overrides to matched (existing) tokens ----------------------

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

            # Reject adapter_id overrides — cannot be changed through env.
            if "adapter_id" in field_map:
                raise ConfigValidationError(
                    "adapter_id cannot be changed through env; "
                    "rename the adapter in TOML."
                )

            existing = transport_dict.get(adapter_key)
            if existing is None:
                raise ConfigValidationError(
                    f"Adapter {adapter_key!r} not found in {transport} config. "
                    f"Env overrides can only modify adapters defined in TOML."
                )

            new_enabled = existing.enabled
            new_config = existing.config
            new_adapter_id = existing.adapter_id
            new_adapter_kind = existing.adapter_kind

            # Separate enabled override from config-field overrides
            # so we can build config_kwargs once.
            if "enabled" in field_map:
                parsed = field_map["enabled"]
                new_enabled = _coerce_bool(parsed.raw_value, parsed.env_var_name)

            # Apply config-field overrides (fields other than enabled).
            config_field_names = {
                k for k in field_map if k != "enabled"
            }
            if config_field_names:
                # Start from existing config values.
                config_kwargs = {
                    f.name: getattr(new_config, f.name)
                    for f in fields(config_cls)
                }

                for field_name in config_field_names:
                    parsed = field_map[field_name]
                    field_type = _get_field_type(transport, field_name)

                    # Reject dict/tuple fields — they cannot be set via env.
                    unwrapped = _unwrap_optional_type(field_type) if field_type else None
                    origin = get_origin(unwrapped) if unwrapped else None
                    if unwrapped in (dict, tuple) or origin in (dict, tuple):
                        raise ConfigValidationError(
                            f"Field {field_name!r} has type "
                            f"{'dict' if (unwrapped is dict or origin is dict) else 'tuple'}"
                            f" and cannot be set through env; "
                            f"configure it in TOML."
                        )

                    coerced = _coerce_field_value(
                        parsed.raw_value, field_name, field_type, parsed.env_var_name
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

    # -- Create new adapters from env tokens with TRANSPORT ----------------

    for token, field_map in created_tokens.items():
        transport_raw = field_map["transport"].raw_value.strip().lower()
        if transport_raw not in _TRANSPORT_REGISTRY:
            supported = sorted(_TRANSPORT_REGISTRY.keys())
            raise ConfigValidationError(
                f"Invalid TRANSPORT {transport_raw!r} for token {token!r}. "
                f"Supported transports: {supported}"
            )

        transport = transport_raw
        config_cls, runtime_cls = _TRANSPORT_REGISTRY[transport]
        valid_fields = _valid_fields_for_transport(transport)

        # Determine adapter_id (explicit or default).
        if "adapter_id" in field_map:
            adapter_id = field_map["adapter_id"].raw_value.strip()
        else:
            adapter_id = token.lower().replace("_", "-")

        # Determine enabled (defaults to True).
        enabled = True
        if "enabled" in field_map:
            enabled = _coerce_bool(
                field_map["enabled"].raw_value,
                field_map["enabled"].env_var_name,
            )

        # Determine adapter_kind (defaults to "real").
        adapter_kind = "real"
        if "adapter_kind" in field_map:
            adapter_kind = field_map["adapter_kind"].raw_value.strip().lower()
            if adapter_kind not in ("real", "fake"):
                raise ConfigValidationError(
                    f"ADAPTER_KIND for token {token!r} must be 'real' or 'fake', "
                    f"got {adapter_kind!r}"
                )

        # Build config kwargs from remaining fields (excluding meta-fields).
        meta_fields = {"transport", "adapter_id", "enabled", "adapter_kind"}
        config_field_names = {k for k in field_map if k not in meta_fields}

        # Validate field names against transport's config class.
        unsupported = config_field_names - valid_fields
        if unsupported:
            raise ConfigValidationError(
                f"Unsupported fields for new {transport} adapter {token!r}: "
                f"{sorted(unsupported)}. Valid fields: {sorted(valid_fields)}"
            )

        # Coerce each config field from its env-var string value.
        config_kwargs: dict[str, Any] = {}
        for field_name in config_field_names:
            parsed = field_map[field_name]
            field_type = _get_field_type(transport, field_name)

            # Reject dict/tuple fields.
            unwrapped = _unwrap_optional_type(field_type) if field_type else None
            origin = get_origin(unwrapped) if unwrapped else None
            if unwrapped in (dict, tuple) or origin in (dict, tuple):
                raise ConfigValidationError(
                    f"Field {field_name!r} has type "
                    f"{'dict' if (unwrapped is dict or origin is dict) else 'tuple'}"
                    f" and cannot be set through env; "
                    f"configure it in TOML."
                )

            coerced = _coerce_field_value(
                parsed.raw_value, field_name, field_type, parsed.env_var_name
            )
            config_kwargs[field_name] = coerced

        config_kwargs["adapter_id"] = adapter_id
        new_config = config_cls(**config_kwargs).validate()

        new_rtc = runtime_cls(
            adapter_id=adapter_id,
            enabled=enabled,
            adapter_kind=adapter_kind,
            config=new_config,
        )
        transport_dicts[transport][adapter_id] = new_rtc

        # Back-fill target_transport on provenance entries for this token.
        if provenance is not None:
            for field_name, parsed in field_map.items():
                entry = provenance._entries.get(parsed.env_var_name)
                if entry is not None and entry.target_transport is None:
                    entry.target_transport = transport

    # -- Normalized-token collision check (existing + newly created) --------

    token_locations: dict[str, list[tuple[str, str, str]]] = {}
    for transport_name, tdict in transport_dicts.items():
        for key, rtc in tdict.items():
            tok = normalize_adapter_id(rtc.adapter_id)
            token_locations.setdefault(tok, []).append(
                (transport_name, key, rtc.adapter_id)
            )
    token_collisions = {
        tok: locs for tok, locs in token_locations.items() if len(locs) > 1
    }
    if token_collisions:
        collision_msgs: list[str] = []
        for tok, locs in token_collisions.items():
            details = "; ".join(
                f"{t}.{k} adapter_id={a!r}" for t, k, a in locs
            )
            collision_msgs.append(
                f"Adapter env token collision for {tok}: {details}. "
                f"Rename one adapter_id."
            )
        raise ConfigValidationError("; ".join(collision_msgs))

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
    # Detect token collisions early — even before checking if any env vars
    # are set.  Two adapters normalizing to the same token is a config error
    # regardless of env overrides.
    refs = _collect_configured_adapter_refs(config)
    collision_err = _check_token_collisions(refs)
    if collision_err:
        raise ConfigValidationError(collision_err)

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

    config = apply_instance_env_overrides(
        config, env.instance_overrides, provenance=env.provenance
    )

    return config
