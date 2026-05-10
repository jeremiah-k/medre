"""Typed configuration models for the MEDRE runtime.

This module defines the frozen-dataclass configuration hierarchy consumed by
the TOML loader (:mod:`medre.config.loader`), environment-variable overrides
(:mod:`medre.config.env`), the runtime builder, and the CLI.

Adapter-specific settings are *wrapped*, not duplicated — each runtime config
type holds a reference to the existing adapter config dataclass from
:mod:`medre.adapters.*.config`.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Self, get_type_hints, get_args

from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.meshtastic.config import MeshtasticConfig
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.lxmf.config import LxmfConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields consumed by the runtime wrapper (not forwarded to adapter configs).
_WRAPPER_FIELD_NAMES: frozenset[str] = frozenset({"enabled", "adapter_id"})


def _coerce_adapter_kwargs(
    config_cls: type,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Filter *raw* to fields accepted by *config_cls* and coerce types.

    TOML produces ``list`` (not ``set``) and string-keyed dicts (not int-keyed).
    This helper inspects field annotations and converts values so the frozen
    dataclass constructor receives what it expects.
    """
    valid_names: frozenset[str] = frozenset(
        f.name for f in dataclasses.fields(config_cls)
    )
    hints = get_type_hints(config_cls)
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in valid_names:
            continue
        hint = hints.get(key)
        # list → set coercion for set-typed fields (e.g. room_allowlist)
        if isinstance(value, list) and _is_set_annotation(hint):
            value = set(value)
        # TOML dicts have string keys; coerce to int if the annotation
        # expects int keys (e.g. channel_mapping: dict[int, str]).
        if isinstance(value, dict) and _is_int_keyed_dict(hint):
            try:
                value = {int(k): v for k, v in value.items()}
            except (ValueError, TypeError):
                pass  # let the adapter config's validate() catch it
        kwargs[key] = value
    return kwargs


def _is_set_annotation(hint: Any) -> bool:
    """Return True if *hint* looks like ``set[...]`` or ``frozenset[...]``.

    Handles bare types and ``X | None`` unions (both ``typing.Union``
    and PEP-604 ``types.UnionType``).
    """
    origin = getattr(hint, "__origin__", None)
    if origin is set or origin is frozenset:
        return True
    # Handle Union types — both typing.Union and types.UnionType (PEP 604).
    args = getattr(hint, "__args__", None)
    if args is not None:
        return any(_is_set_annotation(a) for a in args)
    return False


def _is_int_keyed_dict(hint: Any) -> bool:
    """Return True if *hint* looks like ``dict[int, ...]``.

    Handles bare types and ``X | None`` unions (both ``typing.Union``
    and PEP-604 ``types.UnionType``).
    """
    origin = getattr(hint, "__origin__", None)
    if origin is dict:
        args = get_args(hint)
        return bool(args) and args[0] is int
    # Handle Union types — both typing.Union and types.UnionType (PEP 604).
    args = getattr(hint, "__args__", None)
    if args is not None:
        return any(_is_int_keyed_dict(a) for a in args)
    return False


# ---------------------------------------------------------------------------
# Leaf configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeOptions:
    """Top-level runtime behaviour knobs."""

    name: str = "medre"
    shutdown_timeout_seconds: int = 10


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    format: str = "text"  # "text" or "json"


@dataclass(frozen=True)
class StorageConfig:
    """Persistence / storage configuration."""

    backend: str = "sqlite"
    path: str | None = None  # None → use default: {state}/medre.sqlite


# ---------------------------------------------------------------------------
# Adapter runtime wrappers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixRuntimeConfig:
    """Runtime wrapper for a single Matrix adapter instance."""

    adapter_id: str
    enabled: bool = True
    config: MatrixConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict.

        *instance_name* is the key under ``[adapters.matrix]`` and becomes
        ``adapter_id`` unless the table explicitly provides one.

        Encryption settings (``encryption_mode``, ``ignore_unverified_devices``,
        ``require_encrypted_rooms``) are set directly in the TOML table and
        pass through to :class:`MatrixConfig` via
        :func:`_coerce_adapter_kwargs`.
        """
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kwargs = _coerce_adapter_kwargs(MatrixConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MatrixConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, config=config)


@dataclass(frozen=True)
class MeshtasticRuntimeConfig:
    """Runtime wrapper for a single Meshtastic adapter instance."""

    adapter_id: str
    enabled: bool = True
    config: MeshtasticConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kwargs = _coerce_adapter_kwargs(MeshtasticConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MeshtasticConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, config=config)


@dataclass(frozen=True)
class MeshCoreRuntimeConfig:
    """Runtime wrapper for a single MeshCore adapter instance."""

    adapter_id: str
    enabled: bool = True
    config: MeshCoreConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kwargs = _coerce_adapter_kwargs(MeshCoreConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MeshCoreConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, config=config)


@dataclass(frozen=True)
class LxmfRuntimeConfig:
    """Runtime wrapper for a single LXMF adapter instance."""

    adapter_id: str
    enabled: bool = True
    config: LxmfConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kwargs = _coerce_adapter_kwargs(LxmfConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = LxmfConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, config=config)


# ---------------------------------------------------------------------------
# Adapter collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterConfigSet:
    """Holds all adapter configs grouped by transport type.

    Each mapping key is the adapter *instance name* (used as ``adapter_id``
    unless the instance config overrides it).
    """

    matrix: dict[str, MatrixRuntimeConfig] = field(default_factory=dict)
    meshtastic: dict[str, MeshtasticRuntimeConfig] = field(default_factory=dict)
    meshcore: dict[str, MeshCoreRuntimeConfig] = field(default_factory=dict)
    lxmf: dict[str, LxmfRuntimeConfig] = field(default_factory=dict)

    def all_enabled(self) -> list[tuple[str, object]]:
        """Return ``(adapter_id, config)`` for all enabled adapters."""
        result: list[tuple[str, object]] = []
        for group in (self.matrix, self.meshtastic, self.meshcore, self.lxmf):
            for _name, rtc in group.items():
                if rtc.enabled:
                    result.append((rtc.adapter_id, rtc))
        return result

    def all_configs(self) -> list[tuple[str, str, object]]:
        """Return ``(transport_type, adapter_id, config)`` for all adapters."""
        result: list[tuple[str, str, object]] = []
        for transport, group in (
            ("matrix", self.matrix),
            ("meshtastic", self.meshtastic),
            ("meshcore", self.meshcore),
            ("lxmf", self.lxmf),
        ):
            for _name, rtc in group.items():
                result.append((transport, rtc.adapter_id, rtc))
        return result


# ---------------------------------------------------------------------------
# Root configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeConfig:
    """Top-level runtime configuration.

    This is the single object produced by the TOML loader and consumed by the
    runtime builder and CLI.
    """

    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    adapters: AdapterConfigSet = field(default_factory=AdapterConfigSet)
