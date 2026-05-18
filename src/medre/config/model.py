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
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self, get_type_hints, get_args

from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.errors import ConfigValidationError

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from medre.runtime.routes import RouteConfigSet


def _default_route_config_set() -> RouteConfigSet:
    """Deferred import to avoid circular dependency with runtime.routes."""
    from medre.runtime.routes import RouteConfigSet as _RCS
    return _RCS()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields consumed by the runtime wrapper (not forwarded to adapter configs).
_WRAPPER_FIELD_NAMES: frozenset[str] = frozenset({"enabled", "adapter_id", "adapter_kind"})


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
    """Logging configuration.

    Attributes
    ----------
    level:
        Root logger level (e.g. ``"DEBUG"``, ``"INFO"``).
    format:
        Log format preset — ``"text"`` or ``"json"``.
    overrides:
        Per-logger level overrides keyed by logger name.  Allows
        suppressing noisy SDK loggers (e.g. ``nio``, ``meshtastic``,
        ``aiohttp``) at a higher level than the root logger.  Values
        are level name strings (e.g. ``"WARNING"``).  When empty, the
        CLI layer applies sensible defaults.
    """

    level: str = "INFO"
    format: str = "text"  # "text" or "json"
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RetryConfig:
    """Retry worker configuration.

    Attributes
    ----------
    enabled:
        Whether the background retry worker is active.
    interval_seconds:
        Polling interval in seconds for checking due retry receipts.
    batch_size:
        Maximum number of retry receipts processed per polling cycle.
    max_attempts:
        Maximum total delivery attempts before dead-lettering.
    """

    enabled: bool = False
    interval_seconds: float = 10.0
    batch_size: int = 20
    max_attempts: int = 3


@dataclass(frozen=True)
class StorageConfig:
    """Persistence / storage configuration."""

    backend: str = "sqlite"
    path: str | None = None  # None → use default: {state}/medre.sqlite


@dataclass(frozen=True)
class RuntimeLimits:
    """Runtime resource limits controlling throughput and drain behaviour.

    Fields
    ------
    max_inflight_deliveries:
        Maximum number of deliveries that may be in-flight concurrently.
    max_inflight_replay_events:
        Maximum number of replay events that may be processed concurrently.
    shutdown_drain_timeout_seconds:
        Maximum time (in seconds) to wait for in-flight work to drain
        during graceful shutdown before forcing termination.
    delivery_acquire_timeout_seconds:
        Timeout (in seconds) for acquiring a delivery slot when the
        in-flight limit is reached.
    """

    max_inflight_deliveries: int = 100
    max_inflight_replay_events: int = 100
    shutdown_drain_timeout_seconds: int = 10
    delivery_acquire_timeout_seconds: float = 1.0

    def validate(self) -> Self:
        """Validate runtime limits.

        Raises
        ------
        ConfigValidationError
            If any limit is non-positive.
        """
        if self.max_inflight_deliveries <= 0:
            raise ConfigValidationError(
                f"max_inflight_deliveries must be > 0, got {self.max_inflight_deliveries}"
            )
        if self.max_inflight_replay_events <= 0:
            raise ConfigValidationError(
                f"max_inflight_replay_events must be > 0, got {self.max_inflight_replay_events}"
            )
        if self.shutdown_drain_timeout_seconds <= 0:
            raise ConfigValidationError(
                f"shutdown_drain_timeout_seconds must be > 0, "
                f"got {self.shutdown_drain_timeout_seconds}"
            )
        if self.delivery_acquire_timeout_seconds <= 0:
            raise ConfigValidationError(
                f"delivery_acquire_timeout_seconds must be > 0, "
                f"got {self.delivery_acquire_timeout_seconds}"
            )
        # Reasonable upper-bound warnings (not hard failures).
        _UPPER_BOUND = 10_000
        if self.max_inflight_deliveries > _UPPER_BOUND:
            _logger.warning(
                "max_inflight_deliveries=%d exceeds recommended upper bound (%d); "
                "high concurrency may degrade performance",
                self.max_inflight_deliveries,
                _UPPER_BOUND,
            )
        if self.max_inflight_replay_events > _UPPER_BOUND:
            _logger.warning(
                "max_inflight_replay_events=%d exceeds recommended upper bound (%d); "
                "high concurrency may degrade performance",
                self.max_inflight_replay_events,
                _UPPER_BOUND,
            )
        return self


# ---------------------------------------------------------------------------
# Adapter runtime wrappers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixRuntimeConfig:
    """Runtime wrapper for a single Matrix adapter instance."""

    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "real"
    config: MatrixConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict.

        *instance_name* is the key under ``[adapters.matrix]`` and becomes
        ``adapter_id`` unless the table explicitly provides one.

        Encryption settings (``encryption_mode``,
        ``require_encrypted_rooms``) are set directly in the TOML table and
        pass through to :class:`MatrixConfig` via
        :func:`_coerce_adapter_kwargs`.
        """
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kind: str = data.pop("adapter_kind", "real")
        if adapter_kind not in ("real", "fake"):
            raise ConfigValidationError(
                f"adapter_kind must be 'real' or 'fake', got {adapter_kind!r} "
                f"in adapters.matrix.{instance_name}",
                transport="matrix",
                adapter_id=adapter_id,
                section_path=f"adapters.matrix.{instance_name}",
            )
        adapter_kwargs = _coerce_adapter_kwargs(MatrixConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MatrixConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, adapter_kind=adapter_kind, config=config)


@dataclass(frozen=True)
class MeshtasticRuntimeConfig:
    """Runtime wrapper for a single Meshtastic adapter instance."""

    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "real"
    config: MeshtasticConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kind: str = data.pop("adapter_kind", "real")
        if adapter_kind not in ("real", "fake"):
            raise ConfigValidationError(
                f"adapter_kind must be 'real' or 'fake', got {adapter_kind!r} "
                f"in adapters.meshtastic.{instance_name}",
                transport="meshtastic",
                adapter_id=adapter_id,
                section_path=f"adapters.meshtastic.{instance_name}",
            )
        adapter_kwargs = _coerce_adapter_kwargs(MeshtasticConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MeshtasticConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, adapter_kind=adapter_kind, config=config)


@dataclass(frozen=True)
class MeshCoreRuntimeConfig:
    """Runtime wrapper for a single MeshCore adapter instance."""

    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "real"
    config: MeshCoreConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kind: str = data.pop("adapter_kind", "real")
        if adapter_kind not in ("real", "fake"):
            raise ConfigValidationError(
                f"adapter_kind must be 'real' or 'fake', got {adapter_kind!r} "
                f"in adapters.meshcore.{instance_name}",
                transport="meshcore",
                adapter_id=adapter_id,
                section_path=f"adapters.meshcore.{instance_name}",
            )
        adapter_kwargs = _coerce_adapter_kwargs(MeshCoreConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = MeshCoreConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, adapter_kind=adapter_kind, config=config)


@dataclass(frozen=True)
class LxmfRuntimeConfig:
    """Runtime wrapper for a single LXMF adapter instance."""

    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "real"
    config: LxmfConfig | None = None

    @classmethod
    def from_toml_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict."""
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kind: str = data.pop("adapter_kind", "real")
        if adapter_kind not in ("real", "fake"):
            raise ConfigValidationError(
                f"adapter_kind must be 'real' or 'fake', got {adapter_kind!r} "
                f"in adapters.lxmf.{instance_name}",
                transport="lxmf",
                adapter_id=adapter_id,
                section_path=f"adapters.lxmf.{instance_name}",
            )
        adapter_kwargs = _coerce_adapter_kwargs(LxmfConfig, data)
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = LxmfConfig(**adapter_kwargs).validate()
        return cls(adapter_id=adapter_id, enabled=enabled, adapter_kind=adapter_kind, config=config)


# ---------------------------------------------------------------------------
# Adapter collection
# ---------------------------------------------------------------------------

# Union of all runtime config wrappers — used by AdapterConfigSet methods
# and consumed by the runtime builder and app to access .enabled, .config,
# .adapter_kind without an ``object`` typed return.
AdapterRuntimeConfig = (
    MatrixRuntimeConfig
    | MeshtasticRuntimeConfig
    | MeshCoreRuntimeConfig
    | LxmfRuntimeConfig
)


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

    def all_enabled(self) -> list[tuple[str, AdapterRuntimeConfig]]:
        """Return ``(adapter_id, config)`` for all enabled adapters."""
        result: list[tuple[str, AdapterRuntimeConfig]] = []
        for group in (self.matrix, self.meshtastic, self.meshcore, self.lxmf):
            for _name, rtc in group.items():
                if rtc.enabled:
                    result.append((rtc.adapter_id, rtc))
        return result

    def all_configs(self) -> list[tuple[str, str, AdapterRuntimeConfig]]:
        """Return ``(transport_type, adapter_id, config)`` for all adapters."""
        result: list[tuple[str, str, AdapterRuntimeConfig]] = []
        for transport, group in (
            ("matrix", self.matrix),
            ("meshtastic", self.meshtastic),
            ("meshcore", self.meshcore),
            ("lxmf", self.lxmf),
        ):
            for _name, rtc in group.items():
                result.append((transport, rtc.adapter_id, rtc))
        return result

    def validate(self) -> None:
        """Validate the adapter configuration set for consistency.

        Checks performed:

        * **Duplicate adapter IDs** — no two adapters (even across
          different transports) may share the same ``adapter_id``.
          The ``adapter_id`` determines per-adapter state directories
          and runtime identity, so duplicates would cause path conflicts.

        Raises
        ------
        ConfigValidationError
            If a validation rule is violated.
        """
        # -- Duplicate adapter IDs across all transports ----------------------
        seen: dict[str, tuple[str, str]] = {}  # adapter_id → (transport, instance_name)
        for transport, group in (
            ("matrix", self.matrix),
            ("meshtastic", self.meshtastic),
            ("meshcore", self.meshcore),
            ("lxmf", self.lxmf),
        ):
            for instance_name, rtc in group.items():
                aid = rtc.adapter_id
                if aid in seen:
                    prev_transport, prev_name = seen[aid]
                    section = f"adapters.{transport}.{instance_name}"
                    prev_section = f"adapters.{prev_transport}.{prev_name}"
                    raise ConfigValidationError(
                        f"Duplicate adapter: {transport}.{aid} "
                        f"(also defined as {prev_transport}.{aid}). "
                        f"Adapter IDs must be unique across all transports.",
                        transport=transport,
                        adapter_id=aid,
                        section_path=section,
                    )
                seen[aid] = (transport, instance_name)


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
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    retry: RetryConfig = field(default_factory=RetryConfig)
    adapters: AdapterConfigSet = field(default_factory=AdapterConfigSet)
    routes: RouteConfigSet = field(default_factory=_default_route_config_set)
