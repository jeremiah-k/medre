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
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, Self, get_args, get_type_hints, runtime_checkable

from medre.adapter_registry import registered_transports
from medre.config.errors import ConfigValidationError
from medre.config.identifiers import adapter_id_problem
from medre.config.routes import RouteConfigSet
from medre.core.storage.content_retention import (
    RETENTION_DEFAULT_BATCH,
    RETENTION_DEFAULT_INTERVAL,
    RETENTION_POLICY_VERSION,
    ContentRetentionPolicy,
)

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
    union_args = getattr(hint, "__args__", None)
    if union_args is not None:
        return any(_is_int_keyed_dict(a) for a in union_args)
    return False


# ---------------------------------------------------------------------------
# Leaf configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeOptions:
    """Top-level runtime behaviour knobs."""

    name: str = "medre"
    shutdown_timeout_seconds: int = 10
    adapter_start_timeout_seconds: float = 60.0

    def validate(self) -> Self:
        """Validate operator-supplied options before runtime construction."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigValidationError("runtime.name must be a non-empty string")
        timeout = self.shutdown_timeout_seconds
        if type(timeout) is not int or timeout <= 0:
            raise ConfigValidationError(
                "runtime.shutdown_timeout_seconds must be a positive integer"
            )
        startup_timeout = self.adapter_start_timeout_seconds
        try:
            invalid_startup_timeout = (
                isinstance(startup_timeout, bool)
                or not isinstance(startup_timeout, (int, float))
                or not math.isfinite(startup_timeout)
                or startup_timeout <= 0
            )
        except OverflowError:
            invalid_startup_timeout = True
        if invalid_startup_timeout:
            raise ConfigValidationError(
                "runtime.adapter_start_timeout_seconds must be a finite positive number"
            )
        return self


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
        Whether automatic retry dispatch is active; initial outbox delivery remains active.
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

    def validate(self) -> Self:
        """Reject invalid retry scheduling values for all config entry paths."""
        if type(self.enabled) is not bool:
            raise ConfigValidationError("retry.enabled must be a boolean")
        for name in ("batch_size", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigValidationError(f"retry.{name} must be an integer >= 1")
        interval = self.interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ConfigValidationError(
                "retry.interval_seconds must be a positive finite number"
            )
        try:
            finite = math.isfinite(interval)
        except (OverflowError, ValueError):
            finite = False
        if not finite or interval <= 0:
            raise ConfigValidationError(
                "retry.interval_seconds must be a positive finite number"
            )
        return self


@dataclass(frozen=True)
class AttachmentConfig:
    """Generic durable-attachment policy (disabled unless explicitly enabled).

    Fields
    ------
    enabled:
        Master switch for retaining and transferring attachment bytes.
        When ``False``, no attachment bytes are fetched, stored, or
        delivered; previously retained bytes are not transferred either.
    max_attachment_bytes:
        Maximum measured size of one primary attachment.
    max_retained_bytes:
        Maximum total of unique retained attachment bytes.  Reaching the
        quota rejects new content explicitly; nothing is evicted.  Raising
        or lowering capacity is an operator action — this runtime adds no
        automatic binary retention, TTL, or GC.
    max_concurrent_transfers:
        Maximum number of concurrent binary transfers (downloads and
        uploads combined) across the whole runtime.
    transfer_timeout_seconds:
        Deadline for one binary transfer.
    """

    enabled: bool = False
    max_attachment_bytes: int = 10_485_760  # 10 MiB
    max_retained_bytes: int = 268_435_456  # 256 MiB
    max_concurrent_transfers: int = 2
    transfer_timeout_seconds: float = 60.0

    def validate(self) -> Self:
        """Validate attachment limits.

        Raises
        ------
        ConfigValidationError
            If any limit is not a positive finite number, a boolean is
            supplied where a number is required, or the combination is
            nonsensical (a per-attachment cap above the retained budget,
            or a per-attachment cap smaller than one byte).
        """
        for name in ("max_attachment_bytes", "max_retained_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ConfigValidationError(
                    f"attachments.{name} must be a positive integer, " f"got {value!r}"
                )
        if isinstance(self.max_concurrent_transfers, bool) or (
            not isinstance(self.max_concurrent_transfers, int)
            or self.max_concurrent_transfers < 1
        ):
            raise ConfigValidationError(
                "attachments.max_concurrent_transfers must be an integer >= 1, "
                f"got {self.max_concurrent_transfers!r}"
            )
        timeout = self.transfer_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigValidationError(
                "attachments.transfer_timeout_seconds must be a positive "
                f"finite number, got {timeout!r}"
            )
        try:
            timeout_value = float(timeout)
        except OverflowError as exc:
            raise ConfigValidationError(
                "attachments.transfer_timeout_seconds must be a positive "
                f"finite number, got {timeout!r}"
            ) from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ConfigValidationError(
                "attachments.transfer_timeout_seconds must be a positive "
                f"finite number, got {timeout!r}"
            )
        if self.max_attachment_bytes > self.max_retained_bytes:
            raise ConfigValidationError(
                "attachments.max_attachment_bytes cannot exceed "
                "attachments.max_retained_bytes "
                f"({self.max_attachment_bytes} > {self.max_retained_bytes})"
            )
        return self


@dataclass(frozen=True)
class ContentRetentionConfig:
    """Operator content-retention policy declaration.

    Disabled by default: an omitted section means no retention collector and
    no policy. ``mode="retain"`` is a declared, observing policy that never
    collects; ``mode="relay-only"`` is the explicit opt-in to bounded,
    guarded logical collection and additionally requires ``enabled=True`` —
    the ambiguous disabled relay-only scope is refused at load time.
    """

    enabled: bool = False
    version: int = RETENTION_POLICY_VERSION
    mode: str = "retain"
    interval_seconds: int = RETENTION_DEFAULT_INTERVAL
    batch_limit: int = RETENTION_DEFAULT_BATCH

    def validate(self) -> Self:
        """Validate the retention declaration.

        Raises
        ------
        ConfigValidationError
            If a field has the wrong type (in particular, a boolean is not
            an integer version), the mode is outside the closed vocabulary,
            a cadence knob is out of bounds, or ``relay-only`` is declared
            while the section is disabled.
        """
        if type(self.enabled) is not bool:
            raise ConfigValidationError(
                "content_retention.enabled must be a boolean, got "
                f"{type(self.enabled).__name__}"
            )
        try:
            ContentRetentionPolicy(
                version=self.version,
                mode=self.mode,
                interval_seconds=self.interval_seconds,
                batch_limit=self.batch_limit,
            )
        except ValueError as exc:
            raise ConfigValidationError(f"content_retention.{exc}") from exc
        if self.mode == "relay-only" and not self.enabled:
            raise ConfigValidationError(
                "content_retention.mode='relay-only' requires "
                "content_retention.enabled=true; an ambiguous disabled "
                "relay-only scope is refused"
            )
        return self


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
        The durable outbox dispatcher also limits its native tasks to this
        count and schedules at most one handoff per adapter.
    max_inflight_replay_events:
        Maximum number of replay events that may be processed concurrently.
    shutdown_drain_timeout_seconds:
        Maximum time (in seconds) to wait for in-flight work to drain
        during graceful shutdown before forcing termination.
    delivery_acquire_timeout_seconds:
        Timeout (in seconds) for acquiring a delivery slot when the
        in-flight limit is reached.
    max_inflight_inbound_admissions:
        Maximum number of inbound events that may cross durable
        admission concurrently. Callback-scheduled ingress coroutines
        beyond this bound wait for a slot.
    inbound_admission_timeout_seconds:
        Timeout (in seconds) an inbound event may wait for an admission
        slot before it is rejected with counted evidence.
    """

    max_inflight_deliveries: int = 100
    max_inflight_replay_events: int = 100
    shutdown_drain_timeout_seconds: float = 10.0
    delivery_acquire_timeout_seconds: float = 1.0
    max_inflight_inbound_admissions: int = 100
    inbound_admission_timeout_seconds: float = 5.0

    def validate(self) -> Self:
        """Reject invalid limits before they reach asyncio capacity controls.

        Concurrent-work limits are positive integers; timeouts are positive,
        finite real numbers. In particular, ``bool`` is not an integer limit.
        """
        for name in (
            "max_inflight_deliveries",
            "max_inflight_replay_events",
            "max_inflight_inbound_admissions",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise ConfigValidationError(
                    f"{name} must be a positive integer, got {type(value).__name__}"
                )
            if value <= 0:
                raise ConfigValidationError(f"{name} must be > 0, got {value!r}")

        for name in (
            "shutdown_drain_timeout_seconds",
            "delivery_acquire_timeout_seconds",
            "inbound_admission_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigValidationError(
                    f"{name} must be a positive finite number, got {type(value).__name__}"
                )
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ConfigValidationError(
                    f"{name} must be a positive finite number, got {type(value).__name__}"
                )
            if value <= 0:
                raise ConfigValidationError(f"{name} must be > 0, got {value!r}")
        # Reasonable upper-bound warnings (not hard failures).
        _UPPER_BOUND = 10_000
        if self.max_inflight_deliveries > _UPPER_BOUND:
            _logger.warning(
                "max_inflight_deliveries=%d exceeds recommended upper bound (%d); "
                "high concurrency may degrade performance",
                self.max_inflight_deliveries,
                _UPPER_BOUND,
            )
        if self.max_inflight_inbound_admissions > _UPPER_BOUND:
            _logger.warning(
                "max_inflight_inbound_admissions=%d exceeds recommended upper bound "
                "(%d); high concurrency may degrade performance",
                self.max_inflight_inbound_admissions,
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

    Adapters may provide independent validated wrappers. Core never needs to
    import transport-specific configuration types.
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
        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                f"{section_path}.enabled must be a boolean",
                transport=transport,
                adapter_id=adapter_id,
                section_path=section_path,
            )
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


@runtime_checkable
class AdapterRuntimeConfig(Protocol):
    """Structural lifecycle fields owned wrappers expose to core consumers."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def adapter_kind(self) -> str: ...

    @property
    def config(self) -> Any | None: ...


# ---------------------------------------------------------------------------
# Adapter collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, init=False)
class AdapterConfigSet:
    """Adapter configs grouped by registered transport name.

    The authoritative storage is a transport-keyed mapping. Transport groups
    can be passed through ``groups`` or keyword arguments, so newly installed
    providers require no changes to this model.

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
                f"known installed transports: {sorted(known_set)}. "
                "Install a distribution providing the matching medre.adapters entry point."
            )
        object.__setattr__(
            self,
            "_groups",
            {transport: dict(merged.get(transport, {})) for transport in known},
        )

    def __getattr__(self, name: str) -> dict[str, AdapterRuntimeConfig]:
        """Expose installed transport groups by name."""
        groups: dict[str, dict[str, AdapterRuntimeConfig]] = object.__getattribute__(
            self, "_groups"
        )
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
                section = f"adapters.{transport}.{instance_name}"
                if not isinstance(rtc, AdapterRuntimeConfig):
                    raise ConfigValidationError(
                        f"{section}: expected an adapter runtime configuration",
                        transport=transport,
                        section_path=section,
                    )
                if type(rtc.enabled) is not bool:
                    raise ConfigValidationError(
                        f"{section}.enabled must be a boolean",
                        transport=transport,
                        adapter_id=rtc.adapter_id,
                        section_path=section,
                    )
                if rtc.adapter_kind not in ("real", "fake"):
                    raise ConfigValidationError(
                        f"{section}.adapter_kind must be 'real' or 'fake'",
                        transport=transport,
                        adapter_id=rtc.adapter_id,
                        section_path=section,
                    )
                aid = rtc.adapter_id
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
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    content_retention: ContentRetentionConfig = field(
        default_factory=ContentRetentionConfig
    )
    adapters: AdapterConfigSet = field(default_factory=AdapterConfigSet)
    routes: RouteConfigSet = field(default_factory=_default_route_config_set)
