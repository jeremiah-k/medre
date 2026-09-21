"""Typed configuration models for the MEDRE runtime.

This module defines the frozen-dataclass configuration hierarchy consumed by
the config loader (:mod:`medre.config.loader`), environment-variable overrides
(:mod:`medre.config.env`), the runtime builder, and the CLI.

Adapter-specific settings are *wrapped*, not duplicated — each runtime config
type holds a reference to the existing adapter config dataclass from
:mod:`medre.config.adapters.*`.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self, get_args, get_type_hints

from medre.adapter_registry import registered_transports
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.errors import ConfigValidationError
from medre.config.identifiers import adapter_id_problem
from medre.config.routes import RouteConfigSet

_logger = logging.getLogger(__name__)


def _default_route_config_set() -> RouteConfigSet:
    """Construct default empty RouteConfigSet."""
    return RouteConfigSet()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields consumed by the runtime wrapper (not forwarded to adapter configs).
_WRAPPER_FIELD_NAMES: frozenset[str] = frozenset(
    {"enabled", "adapter_id", "adapter_kind"}
)


def _coerce_adapter_kwargs(
    config_cls: type,
    raw: dict[str, Any],
    *,
    transport: str,
    section_path: str,
) -> dict[str, Any]:
    """Filter *raw* to fields accepted by *config_cls* and coerce types.

    YAML produces ``list`` (not ``set``) and string-keyed dicts (not int-keyed).
    This helper inspects field annotations and converts values so the frozen
    dataclass constructor receives what it expects.

    Parameters
    ----------
    config_cls:
        The adapter config dataclass (e.g. :class:`MatrixConfig`).
    raw:
        Remaining adapter table after wrapper-level fields
        (``enabled``, ``adapter_id``, ``adapter_kind``) have been popped.
    transport:
        Transport name (``"matrix"``, ``"meshtastic"``, ...) for error
        messages.
    section_path:
        Dot-separated config path (e.g. ``"adapters.matrix.main"``) for
        error messages.

    Raises
    ------
    ConfigValidationError
        If *raw* contains any key that is not a field of *config_cls*.
        This matches ``additionalProperties: false`` on the adapter JSON
        schemas so a typo (e.g. ``conection_type``) surfaces at load time
        instead of silently falling back to the field default.
    """
    valid_names: frozenset[str] = frozenset(
        f.name for f in dataclasses.fields(config_cls)
    )
    unknown = set(raw) - valid_names
    if unknown:
        msg = (
            f"{section_path}: unknown adapter config key(s) "
            f"{sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))}. "
            f"Accepted keys: {sorted(valid_names)}"
        )
        raise ConfigValidationError(
            msg,
            transport=transport,
            section_path=section_path,
        )
    hints = get_type_hints(config_cls)
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        hint = hints.get(key)
        # list → set coercion for set-typed fields (e.g. room_allowlist)
        if isinstance(value, list) and _is_set_annotation(hint):
            value = set(value)
        # list → tuple coercion for tuple-typed fields (e.g. auto_join_rooms)
        if isinstance(value, list) and _is_tuple_annotation(hint):
            value = tuple(value)
        # YAML dicts have string keys; coerce to int if the annotation
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


def _is_tuple_annotation(hint: Any) -> bool:
    """Return True if *hint* looks like ``tuple[...]``.

    Handles bare types and ``X | None`` unions (both ``typing.Union``
    and PEP-604 ``types.UnionType``).
    """
    origin = getattr(hint, "__origin__", None)
    if origin is tuple:
        return True
    # Handle Union types — both typing.Union and types.UnionType (PEP 604).
    args = getattr(hint, "__args__", None)
    if args is not None:
        return any(_is_tuple_annotation(a) for a in args)
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
        MEDRE namespace logger level (e.g. ``"DEBUG"``, ``"INFO"``).
        Controls the ``medre.*`` logger hierarchy only — dependency
        loggers are governed by their own defaults and overrides.
    format:
        Log format preset — ``"text"`` or ``"json"``.
    overrides:
        Per-logger namespace level overrides keyed by logger name.
        Allows suppressing or enabling output from dependency loggers
        (e.g. ``nio``, ``meshtastic``, ``aiohttp``) independently of
        the MEDRE namespace level.  Values are level name strings
        (e.g. ``"WARNING"``, ``"DEBUG"``).  Validated at config-load
        time; invalid values raise :class:`ConfigValidationError`.
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
        Polling interval in seconds for checking due outbox work.
    batch_size:
        Maximum number of due outbox items claimed per polling cycle.
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
class GenericAdapterRuntimeConfig:
    """Transport-neutral wrapper for one configured adapter instance.

    Concrete built-ins retain thin compatibility subclasses below, but the
    loader can use this class directly for registered adapters without a
    compatibility wrapper.  That keeps the
    runtime wrapper shape stable without requiring :mod:`medre.config.model`
    changes whenever a new transport is registered.
    """

    adapter_id: str
    enabled: bool = True
    adapter_kind: str = "real"
    config: Any | None = None

    @classmethod
    def from_transport_dict(
        cls,
        instance_name: str,
        data: dict[str, Any],
        *,
        transport: str,
        config_cls: type,
    ) -> Self:
        """Construct and validate a runtime wrapper from an adapter config table.

        Wrapper fields are removed before the remaining values are coerced for
        *config_cls*. Raises :class:`ConfigValidationError` for an unsupported
        adapter kind or invalid adapter configuration.
        """
        data = dict(data)
        enabled: bool = data.pop("enabled", True)
        adapter_id: str = data.pop("adapter_id", instance_name)
        adapter_kind: str = data.pop("adapter_kind", "real")
        section_path = f"adapters.{transport}.{instance_name}"
        if adapter_kind not in ("real", "fake"):
            raise ConfigValidationError(
                f"adapter_kind must be 'real' or 'fake', got {adapter_kind!r} "
                f"in {section_path}",
                transport=transport,
                adapter_id=adapter_id,
                section_path=section_path,
            )
        adapter_kwargs = _coerce_adapter_kwargs(
            config_cls,
            data,
            transport=transport,
            section_path=section_path,
        )
        adapter_kwargs.setdefault("adapter_id", adapter_id)
        config = config_cls(**adapter_kwargs).validate()
        return cls(
            adapter_id=adapter_id,
            enabled=enabled,
            adapter_kind=adapter_kind,
            config=config,
        )


@dataclass(frozen=True)
class MatrixRuntimeConfig(GenericAdapterRuntimeConfig):
    """Compatibility wrapper for a single Matrix adapter instance."""

    config: MatrixConfig | None = None

    @classmethod
    def from_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        return cls.from_transport_dict(
            instance_name, data, transport="matrix", config_cls=MatrixConfig
        )


@dataclass(frozen=True)
class MeshtasticRuntimeConfig(GenericAdapterRuntimeConfig):
    """Compatibility wrapper for a single Meshtastic adapter instance."""

    config: MeshtasticConfig | None = None

    @classmethod
    def from_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        return cls.from_transport_dict(
            instance_name, data, transport="meshtastic", config_cls=MeshtasticConfig
        )


@dataclass(frozen=True)
class MeshCoreRuntimeConfig(GenericAdapterRuntimeConfig):
    """Compatibility wrapper for a single MeshCore adapter instance."""

    config: MeshCoreConfig | None = None

    @classmethod
    def from_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        return cls.from_transport_dict(
            instance_name, data, transport="meshcore", config_cls=MeshCoreConfig
        )


@dataclass(frozen=True)
class LxmfRuntimeConfig(GenericAdapterRuntimeConfig):
    """Compatibility wrapper for a single LXMF adapter instance."""

    config: LxmfConfig | None = None

    @classmethod
    def from_dict(cls, instance_name: str, data: dict[str, Any]) -> Self:
        return cls.from_transport_dict(
            instance_name, data, transport="lxmf", config_cls=LxmfConfig
        )


# Registered adapters without a dedicated compatibility wrapper use the
# transport-neutral runtime config directly.
AdapterRuntimeConfig = GenericAdapterRuntimeConfig


# ---------------------------------------------------------------------------
# Adapter collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class AdapterConfigSet:
    """Adapter configs grouped by registered transport name.

    The authoritative storage is a transport-keyed mapping rather than one
    field per built-in adapter.  The custom constructor intentionally accepts
    the historical ``AdapterConfigSet(matrix=..., meshtastic=...)`` spelling
    so existing callers keep working, while a newly registered transport can
    be supplied without editing this class.

    Note that the private ``_groups`` field is the only dataclass field, so
    ``dataclasses.asdict()`` on this type (or on a root :class:`RuntimeConfig`
    containing one) exposes ``{"_groups": ...}`` rather than per-transport
    keys. Serialize via :meth:`groups` instead.
    """

    _groups: dict[str, dict[str, AdapterRuntimeConfig]]

    def __init__(
        self,
        groups: Mapping[str, Mapping[str, AdapterRuntimeConfig]] | None = None,
        **transport_groups: Mapping[str, AdapterRuntimeConfig],
    ) -> None:
        known = registered_transports()
        known_set = set(known)
        merged: dict[str, Mapping[str, AdapterRuntimeConfig]] = dict(groups or {})
        overlap = set(merged) & set(transport_groups)
        if overlap:
            raise TypeError(
                f"adapter transport group(s) supplied twice: {sorted(overlap)}"
            )
        merged.update(transport_groups)
        unknown = set(merged) - known_set
        if unknown:
            raise TypeError(
                f"unknown adapter transport group(s): {sorted(unknown)}; "
                f"known transports: {sorted(known_set)}"
            )
        object.__setattr__(
            self,
            "_groups",
            {transport: dict(merged.get(transport, {})) for transport in known},
        )

    def __getattr__(self, name: str) -> dict[str, AdapterRuntimeConfig]:
        """Expose registered transport groups as compatibility attributes."""
        groups = object.__getattribute__(self, "_groups")
        if name in groups:
            return groups[name]
        raise AttributeError(name)

    def for_transport(self, transport: str) -> dict[str, AdapterRuntimeConfig]:
        """Return the configured instances for one registered transport."""
        try:
            return self._groups[transport]
        except KeyError as exc:
            raise KeyError(
                f"unknown adapter transport {transport!r}; "
                f"known transports: {sorted(self._groups)}"
            ) from exc

    def groups(self) -> tuple[tuple[str, dict[str, AdapterRuntimeConfig]], ...]:
        """Return ``(transport, instances)`` pairs in registry order."""
        return tuple(
            (transport, self._groups[transport])
            for transport in registered_transports()
        )

    def all_enabled(self) -> list[tuple[str, AdapterRuntimeConfig]]:
        """Return ``(adapter_id, config)`` for all enabled adapters."""
        result: list[tuple[str, AdapterRuntimeConfig]] = []
        for _transport, group in self.groups():
            for rtc in group.values():
                if rtc.enabled:
                    result.append((rtc.adapter_id, rtc))
        return result

    def all_configs(self) -> list[tuple[str, str, AdapterRuntimeConfig]]:
        """Return ``(transport_type, adapter_id, config)`` for all adapters."""
        result: list[tuple[str, str, AdapterRuntimeConfig]] = []
        for transport, group in self.groups():
            for rtc in group.values():
                result.append((transport, rtc.adapter_id, rtc))
        return result

    def validate(self) -> None:
        """Validate identifiers, uniqueness, and environment-token safety."""
        from medre.config.env import normalize_adapter_id

        seen: dict[str, tuple[str, str]] = {}
        tokens: dict[str, tuple[str, str]] = {}
        for transport, group in self.groups():
            for instance_name, rtc in group.items():
                aid = rtc.adapter_id
                section = f"adapters.{transport}.{instance_name}"
                instance_problem = adapter_id_problem(instance_name)
                if instance_problem is not None:
                    raise ConfigValidationError(
                        f"{section}: invalid adapter instance name: "
                        f"{instance_problem}.",
                        transport=transport,
                        adapter_id=instance_name,
                        section_path=section,
                    )
                problem = adapter_id_problem(aid)
                if problem is not None:
                    raise ConfigValidationError(
                        f"{section}: {problem}.",
                        transport=transport,
                        adapter_id=aid,
                        section_path=section,
                    )
                if aid in seen:
                    prev_transport, prev_name = seen[aid]
                    raise ConfigValidationError(
                        f"Duplicate adapter: {transport}.{aid} "
                        f"(also defined as {prev_transport}.{prev_name}). "
                        f"Adapter IDs must be unique across all transports.",
                        transport=transport,
                        adapter_id=aid,
                        section_path=section,
                    )
                seen[aid] = (transport, instance_name)
                token = normalize_adapter_id(aid)
                if token in tokens:
                    prev_transport, prev_name = tokens[token]
                    raise ConfigValidationError(
                        f"Adapter env token collision for {token}: "
                        f"{transport}.{instance_name} (adapter_id={aid!r}) "
                        f"and {prev_transport}.{prev_name} both normalize "
                        f"to the same MEDRE_ADAPTER__{token} token. "
                        f"Rename one adapter_id.",
                        transport=transport,
                        adapter_id=aid,
                        section_path=section,
                    )
                tokens[token] = (transport, instance_name)


# ---------------------------------------------------------------------------
# Root runtime config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeConfig:
    """Top-level runtime configuration.

    This is the single object produced by the config loader and consumed by the
    runtime builder and CLI.
    """

    runtime: RuntimeOptions = field(default_factory=RuntimeOptions)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    retry: RetryConfig = field(default_factory=RetryConfig)
    adapters: AdapterConfigSet = field(default_factory=AdapterConfigSet)
    routes: RouteConfigSet = field(default_factory=_default_route_config_set)
