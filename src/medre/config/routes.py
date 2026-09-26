"""Static route and bridge-policy models for the MEDRE config layer.

This module defines the deterministic, immutable data structures that
describe named routes between adapters — the configuration-level view
consumed by the config loader (:mod:`medre.config.loader`) and later by
the runtime builder.

It is deliberately **transport-agnostic**: adapter IDs, event kinds,
channel IDs, and sender IDs are plain strings with no SDK imports.

This module is the canonical home for route config dataclasses.
:mod:`medre.config.route_expansion` owns route expansion (config →
core :class:`~medre.core.routing.models.Route` legs); it imports from
this module.  :mod:`medre.config` must not import from
:mod:`medre.runtime`.

Public symbols
--------------
* :class:`RouteDirectionality` — direction of flow between source/dest
* :class:`BridgePolicy` — static allowlist policy for a route
* :class:`RouteRetryConfig` — per-route retry policy for transient failures
* :class:`RouteDestinationConfig` — structured destination addressing
* :class:`ContextMapEntry` — one ``context_map`` entry (source context → dest)
* :class:`RouteConfig` — a single named route definition
* :class:`RouteConfigSet` — ordered, validated collection of routes
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Self

from medre.config.errors import ConfigValidationError

# ---------------------------------------------------------------------------
# Directionality enum
# ---------------------------------------------------------------------------


class RouteDirectionality(Enum):
    """Direction of event flow between source and destination adapters.

    Values correspond to the ``directionality`` config key in
    ``[routes.<id>]`` sections.
    """

    SOURCE_TO_DEST = "source_to_dest"
    DEST_TO_SOURCE = "dest_to_source"
    BIDIRECTIONAL = "bidirectional"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_VALID_ROUTE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_route_id(route_id: str, *, section_path: str) -> None:
    """Raise :class:`ConfigValidationError` if *route_id* is invalid."""
    if not route_id:
        raise ConfigValidationError(
            "Route ID must not be empty",
            section_path=section_path,
        )
    if not _VALID_ROUTE_ID.match(route_id):
        raise ConfigValidationError(
            f"Invalid route ID {route_id!r}: must contain only "
            f"alphanumeric characters, underscores, or hyphens",
            section_path=section_path,
        )


# ---------------------------------------------------------------------------
# Bridge policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgePolicy:
    """Static allowlist policy attached to a route.

    All fields default to empty tuples, meaning "no restriction".
    An empty tuple is interpreted as "allow everything" for that
    dimension.

    Attributes
    ----------
    allowed_event_types:
        Event kinds this policy permits (e.g. ``("message.created",)``).
    allowed_source_adapters:
        Source adapter IDs this policy permits.
    allowed_dest_adapters:
        Destination adapter IDs this policy permits.
    room_allowlist:
        Room IDs the policy permits (transport-specific targeting).
    channel_allowlist:
        Channel identifiers the policy permits.
    sender_allowlist:
        Sender identifiers the policy permits.
    """

    allowed_event_types: tuple[str, ...] = ()
    allowed_source_adapters: tuple[str, ...] = ()
    allowed_dest_adapters: tuple[str, ...] = ()
    room_allowlist: tuple[str, ...] = ()
    channel_allowlist: tuple[str, ...] = ()
    sender_allowlist: tuple[str, ...] = ()

    # Canonical field names accepted in the policy config table.
    _KNOWN_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "allowed_event_types",
            "allowed_source_adapters",
            "allowed_dest_adapters",
            "room_allowlist",
            "channel_allowlist",
            "sender_allowlist",
        }
    )

    # Allowlist fields that must be lists of strings.
    _ALLOWLIST_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("allowed_source_adapters", "source adapter IDs"),
        ("allowed_dest_adapters", "destination adapter IDs"),
        ("room_allowlist", "room IDs"),
        ("channel_allowlist", "channel IDs"),
        ("sender_allowlist", "sender IDs"),
        ("allowed_event_types", "event types"),
    )

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        route_id: str = "",
        section_path: str = "",
    ) -> Self:
        """Construct from a config dict (the ``[routes.<id>.policy]`` section).

        Parameters
        ----------
        data:
            The parsed dict for the policy section.
        route_id:
            Route ID for error messages (optional).
        section_path:
            Dot-separated config path for error messages (optional).

        Raises
        ------
        ConfigValidationError
            If unknown keys are present, or any allowlist value is not
            a list or tuple of strings (e.g. a bare string which would
            silently become a tuple of characters).
        """
        # Normalized policy section path for consistent error messages.
        policy_path = f"{section_path}.policy" if section_path else "policy"

        # Reject unknown keys so operators don't silently misconfigure.
        unknown = set(data.keys()) - cls._KNOWN_FIELDS
        if unknown:
            _ctx = f"Route {route_id!r}: " if route_id else ""
            msg = (
                f"{_ctx}Unknown policy key(s) {sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))} in "
                f"{policy_path}. Accepted keys: "
                f"{sorted(cls._KNOWN_FIELDS)}"
            )
            raise ConfigValidationError(msg, section_path=policy_path)

        # Validate each allowlist field is a list or tuple of strings.
        for field_name, _label in cls._ALLOWLIST_FIELDS:
            raw = data.get(field_name)
            if raw is None:
                continue
            if isinstance(raw, str):
                raise ConfigValidationError(
                    f"Route {route_id!r}: policy.{field_name} must be a list, "
                    f"not a string. Did you mean [{raw!r}]?",
                    section_path=policy_path,
                )
            if not isinstance(raw, (list, tuple)):
                raise ConfigValidationError(
                    f"Route {route_id!r}: policy.{field_name} must be a list, "
                    f"got {type(raw).__name__}",
                    section_path=policy_path,
                )
            for i, item in enumerate(raw):
                if not isinstance(item, str):
                    raise ConfigValidationError(
                        f"Route {route_id!r}: policy.{field_name}[{i}] must be "
                        f"a string, got {type(item).__name__}: {item!r}",
                        section_path=policy_path,
                    )

        return cls(
            allowed_event_types=tuple(data.get("allowed_event_types", [])),
            allowed_source_adapters=tuple(data.get("allowed_source_adapters", [])),
            allowed_dest_adapters=tuple(data.get("allowed_dest_adapters", [])),
            room_allowlist=tuple(data.get("room_allowlist", [])),
            channel_allowlist=tuple(data.get("channel_allowlist", [])),
            sender_allowlist=tuple(data.get("sender_allowlist", [])),
        )


# ---------------------------------------------------------------------------
# Policy validation helper
# ---------------------------------------------------------------------------


def _validate_policy(
    policy: BridgePolicy,
    *,
    route_id: str,
    section_path: str,
) -> None:
    """Validate a :class:`BridgePolicy` after construction.

    allowed_event_types maps to RouteSource.event_kinds during route
    expansion. allowed_source_adapters, allowed_dest_adapters,
    sender_allowlist, room_allowlist, and channel_allowlist are
    enforced by the route-policy evaluator during delivery planning.

    This function is retained as a validation hook for future
    cross-field consistency checks.
    """
    # Intentionally empty: structural validation (unknown keys, type
    # checking, per-element string checks) is performed in
    # BridgePolicy.from_dict before this function is called.


# ---------------------------------------------------------------------------
# Route retry config
# ---------------------------------------------------------------------------


# Canonical field names accepted in a ``[routes.<id>.retry]`` section.
_RETRY_KNOWN_FIELDS: frozenset[str] = frozenset(
    {"enabled", "max_attempts", "backoff_base", "max_delay_seconds", "jitter"}
)


@dataclass(frozen=True)
class RouteRetryConfig:
    """Per-route retry policy for transient delivery failures.

    When ``enabled`` is ``True``, transient adapter failures on this route
    produce retry receipts with ``next_retry_at`` populated.  The global
    ``[retry]`` section controls whether the :class:`RetryWorker` processes
    them — route retry governs *scheduling*, global retry governs
    *execution*.

    Attributes
    ----------
    enabled:
        Whether retry scheduling is active for this route.
    max_attempts:
        Maximum total delivery attempts (including the initial attempt).
        Must be > 0.
    backoff_base:
        Base delay in seconds for exponential backoff.  Must be >= 0.
    max_delay_seconds:
        Upper bound for the computed backoff delay.  Must be >= 0.
    jitter:
        Whether to add jitter to the backoff delay.
    """

    enabled: bool = True
    max_attempts: int = 3
    backoff_base: float = 2.0
    max_delay_seconds: float = 60.0
    jitter: bool = False

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        route_id: str,
        section_path: str,
    ) -> Self:
        """Construct from a ``[routes.<id>.retry]`` config dict.

        Parameters
        ----------
        data:
            The parsed dict for the retry section.
        route_id:
            The route ID (for error messages).
        section_path:
            Dot-separated config path (for error messages).

        Raises
        ------
        ConfigValidationError
            If values are invalid.
        """
        # Reject unknown keys so a typo like ``max_attempt`` (singular)
        # produces the unknown-key error rather than confusing
        # ``"retry.max_attempts must be >0, got None"`` after the field
        # is read with a default and then rejected by the type check.
        # Mirrors :meth:`BridgePolicy.from_dict` and the JSON schemas'
        # ``additionalProperties: false``.
        unknown = set(data) - _RETRY_KNOWN_FIELDS
        if unknown:
            msg = (
                f"Route {route_id!r}: unknown retry key(s) "
                f"{sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))} in "
                f"{section_path}.retry. Accepted keys: "
                f"{sorted(_RETRY_KNOWN_FIELDS)}"
            )
            raise ConfigValidationError(msg, section_path=f"{section_path}.retry")

        enabled: bool = data.get("enabled", True)
        max_attempts = data.get("max_attempts", 3)
        backoff_base = data.get("backoff_base", 2.0)
        max_delay_seconds = data.get("max_delay_seconds", 60.0)
        jitter: bool = data.get("jitter", False)

        if not isinstance(enabled, bool):
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.enabled must be a boolean, "
                f"got {type(enabled).__name__}",
                section_path=f"{section_path}.retry",
            )
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.max_attempts must be an integer, "
                f"got {max_attempts!r}",
                section_path=f"{section_path}.retry",
            )
        if max_attempts <= 0:
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.max_attempts must be > 0, "
                f"got {max_attempts}",
                section_path=f"{section_path}.retry",
            )
        if not isinstance(backoff_base, (int, float)) or isinstance(backoff_base, bool):
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.backoff_base must be a number, "
                f"got {backoff_base!r}",
                section_path=f"{section_path}.retry",
            )
        if backoff_base < 0:
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.backoff_base must be >= 0, "
                f"got {backoff_base}",
                section_path=f"{section_path}.retry",
            )
        if not isinstance(max_delay_seconds, (int, float)) or isinstance(
            max_delay_seconds, bool
        ):
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.max_delay_seconds must be a number, "
                f"got {max_delay_seconds!r}",
                section_path=f"{section_path}.retry",
            )
        if max_delay_seconds < 0:
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.max_delay_seconds must be >= 0, "
                f"got {max_delay_seconds}",
                section_path=f"{section_path}.retry",
            )
        if not isinstance(jitter, bool):
            raise ConfigValidationError(
                f"Route {route_id!r}: retry.jitter must be a boolean, "
                f"got {type(jitter).__name__}",
                section_path=f"{section_path}.retry",
            )

        return cls(
            enabled=enabled,
            max_attempts=max_attempts,
            backoff_base=float(backoff_base),
            max_delay_seconds=float(max_delay_seconds),
            jitter=jitter,
        )


# ---------------------------------------------------------------------------
# Context-map entry (per-entry structured value for context_map)
# ---------------------------------------------------------------------------


# Canonical field names accepted in a structured context_map entry.
_CONTEXT_MAP_ENTRY_KNOWN_KEYS: frozenset[str] = frozenset(
    {"dest_context", "dest_destination", "source_origin_label", "dest_origin_label"}
)


def _validate_context_map_key(
    raw_key: Any,
    *,
    route_id: str,
    section_path: str,
) -> str:
    """Validate a single ``context_map`` key and return its string form.

    Context keys are opaque strings — no transport-specific syntax is
    validated here (adapters own their transport validation).  Keys must
    already be stripped/normalized: an unstripped key such as ``" 0"``
    is rejected rather than silently normalized so that YAML/TOML
    spellings and programmatic keys cannot drift apart.

    Boolean keys are rejected explicitly (bool-before-str pattern);
    integer keys are accepted in parsed config and normalized to their
    decimal string form (direct construction must use the string form).

    Returns
    -------
    str
        The validated key in its string form.

    Raises
    ----------
    ConfigValidationError
        If the key is a boolean, not a string/integer, empty, or not
        already stripped.
    """
    if isinstance(raw_key, bool):
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map key {raw_key!r} is a boolean, "
            "expected a string",
            section_path=section_path,
        )
    if isinstance(raw_key, int):
        key = str(raw_key)
    elif isinstance(raw_key, str):
        key = raw_key
    else:
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map key {raw_key!r} is not a string, "
            f"got {type(raw_key).__name__}",
            section_path=section_path,
        )
    if not key.strip():
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map key {raw_key!r} must be a "
            "non-empty string",
            section_path=section_path,
        )
    if key != key.strip():
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map key {raw_key!r} must already be "
            f"stripped/normalized; use {key.strip()!r}",
            section_path=section_path,
        )
    return key


def _validate_context_value(
    value: Any,
    *,
    context: str,
    section_path: str | None = None,
) -> str:
    """Validate an opaque context value: a non-empty, already-stripped string.

    Like context_map *keys*, context values are opaque and must already
    be normalized — an unstripped value would silently fail the
    string-equality routing match against adapter-supplied channel IDs.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ConfigValidationError(
            f"{context} must be a string, got {type(value).__name__}: {value!r}",
            section_path=section_path,
        )
    if not value.strip():
        raise ConfigValidationError(
            f"{context} must be a non-empty string",
            section_path=section_path,
        )
    if value != value.strip():
        raise ConfigValidationError(
            f"{context} {value!r} must already be stripped/normalized; "
            f"use {value.strip()!r}",
            section_path=section_path,
        )
    return value


def _normalize_optional_origin_label(
    value: Any,
    *,
    field_name: str,
    context: str,
    section_path: str | None = None,
) -> str | None:
    """Validate an optional per-entry origin label through one code path."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigValidationError(
            f"{context}: {field_name!r} must be a string, got boolean {value!r}",
            section_path=section_path,
        )
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"{context}: {field_name!r} must be a string, "
            f"got {type(value).__name__}",
            section_path=section_path,
        )
    return value


@dataclass(frozen=True)
class ContextMapEntry:
    """One ``context_map`` entry: source-side opaque context -> dest side.

    Each entry maps one source-side context (the ``context_map`` key) to
    the dest side of the bridge: either another opaque context
    (``dest_context``) or a structured destination
    (``dest_destination``) — exactly one of the two.  Entries optionally
    carry ``source_origin_label`` / ``dest_origin_label`` that override
    the route-level labels for this entry's forward/reverse legs.

    Attributes
    ----------
    dest_context:
        Opaque dest-side context string (e.g. a room ID, channel index,
        or topic).  Mutually exclusive with ``dest_destination``.
    dest_destination:
        Structured destination addressing
        (:class:`RouteDestinationConfig`).  Mutually exclusive with
        ``dest_context``; entries carrying one require route
        directionality ``source_to_dest``.
    source_origin_label:
        Per-entry forward-leg source label.  ``None`` means "fall back
        to the route-level ``source_origin_label``".  An explicit ``""``
        means "suppress the adapter-level fallback for this entry's
        forward leg".
    dest_origin_label:
        Per-entry reverse-leg source label. Same semantics as
        ``source_origin_label`` but applied when the direction is
        swapped during expansion.

    Direct construction and parsed configuration use the same
    :class:`ConfigValidationError` invariant family. Parsed route errors also
    carry the route section path.
    """

    dest_context: str | None = None
    dest_destination: RouteDestinationConfig | None = None
    source_origin_label: str | None = None
    dest_origin_label: str | None = None

    def __post_init__(self) -> None:
        if (self.dest_context is None) == (self.dest_destination is None):
            raise ConfigValidationError(
                "context_map entry must set exactly one of 'dest_context' "
                "or 'dest_destination'"
            )
        if self.dest_context is not None:
            object.__setattr__(
                self,
                "dest_context",
                _validate_context_value(
                    self.dest_context,
                    context="context_map entry 'dest_context'",
                ),
            )
        if self.dest_destination is not None and not isinstance(
            self.dest_destination, RouteDestinationConfig
        ):
            raise ConfigValidationError(
                "context_map entry 'dest_destination' must be a "
                f"RouteDestinationConfig, got "
                f"{type(self.dest_destination).__name__}"
            )
        for field_name, value in (
            ("source_origin_label", self.source_origin_label),
            ("dest_origin_label", self.dest_origin_label),
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_origin_label(
                    value,
                    field_name=field_name,
                    context="context_map entry",
                ),
            )


def _parse_context_map_entry(
    raw_value: Any,
    *,
    route_id: str,
    key: str,
    section_path: str,
) -> ContextMapEntry:
    """Parse one structured ``context_map`` entry value.

    ``raw_value`` must be a mapping with exactly one of ``dest_context``
    (opaque non-empty stripped string) or ``dest_destination``
    (structured destination table), plus optional
    ``source_origin_label`` / ``dest_origin_label``.  Unknown keys are
    rejected.  Labels use the bool-before-str check pattern.

    Raises
    ------
    ConfigValidationError
        On shape violations; errors carry section path
        ``<section_path>.context_map.<key>``.
    """
    entry_path = f"{section_path}.context_map.{key}"
    if not isinstance(raw_value, dict):
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map entry for context {key!r} must "
            f"be a table/object, got {type(raw_value).__name__}",
            section_path=entry_path,
        )

    unknown = set(raw_value.keys()) - _CONTEXT_MAP_ENTRY_KNOWN_KEYS
    if unknown:
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map entry for context {key!r} has "
            f"unknown key(s) {sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))}. "
            f"Accepted keys: {sorted(_CONTEXT_MAP_ENTRY_KNOWN_KEYS)}",
            section_path=entry_path,
        )

    dest_context_raw = raw_value.get("dest_context")
    dest_destination_raw = raw_value.get("dest_destination")
    if (dest_context_raw is None) == (dest_destination_raw is None):
        raise ConfigValidationError(
            f"Route {route_id!r}: context_map entry for context {key!r} must "
            f"set exactly one of 'dest_context' or 'dest_destination'",
            section_path=entry_path,
        )

    context = f"Route {route_id!r}: context_map entry for context {key!r}"
    dest_context: str | None = None
    if dest_context_raw is not None:
        dest_context = _validate_context_value(
            dest_context_raw,
            context=f"{context}: 'dest_context'",
            section_path=entry_path,
        )

    dest_destination: RouteDestinationConfig | None = None
    if dest_destination_raw is not None:
        dest_destination = RouteDestinationConfig.from_dict(
            route_id,
            dest_destination_raw,
            field_name="dest_destination",
            section_path=entry_path,
        )

    source_label = _normalize_optional_origin_label(
        raw_value.get("source_origin_label"),
        field_name="source_origin_label",
        context=context,
        section_path=entry_path,
    )
    dest_label = _normalize_optional_origin_label(
        raw_value.get("dest_origin_label"),
        field_name="dest_origin_label",
        context=context,
        section_path=entry_path,
    )
    return ContextMapEntry(
        dest_context=dest_context,
        dest_destination=dest_destination,
        source_origin_label=source_label,
        dest_origin_label=dest_label,
    )


def _validate_context_map_route(rc: RouteConfig) -> None:
    """Validate route-level ``context_map`` invariants after construction.

    Shared by :meth:`RouteConfig.from_dict` and direct construction so
    both paths enforce the identical invariant family (construction
    parity).  Runs after room→channel aliasing, so the channel checks
    cover ``source_room``/``dest_room`` too.

    Checks:

    * map shape — a non-empty dict of stripped string keys mapped to
      :class:`ContextMapEntry` instances;
    * mutual exclusion with ``source_channel``/``dest_channel`` (the
      room aliases fold into these) and route-level ``dest_destination``;
    * exactly one source adapter and one dest adapter;
    * entries carrying ``dest_destination`` require directionality
      ``source_to_dest``;
    * duplicate ``dest_context`` values are rejected when the
      directionality creates reverse (dest→source) legs — they would
      become ambiguous reverse-leg inbound source contexts.  They stay
      valid fan-in for forward-only (``source_to_dest``) routes.
    """
    assert rc.context_map is not None  # guarded by caller
    section_path = f"routes.{rc.route_id}"

    if not isinstance(rc.context_map, dict):
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: context_map must be a dict",
            section_path=section_path,
        )
    if not rc.context_map:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: context_map must not be empty",
            section_path=section_path,
        )
    for raw_key, entry in rc.context_map.items():
        key_str = _validate_context_map_key(
            raw_key,
            route_id=rc.route_id,
            section_path=f"{section_path}.context_map.{raw_key}",
        )
        if key_str != raw_key:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map key {raw_key!r} must use "
                f"normalized string form {key_str!r}",
                section_path=f"{section_path}.context_map.{raw_key}",
            )
        if not isinstance(entry, ContextMapEntry):
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map entry for context "
                f"{raw_key!r} must be a structured entry with exactly one of "
                f"'dest_context' or 'dest_destination'; direct construction "
                f"requires ContextMapEntry, got {type(entry).__name__}",
                section_path=f"{section_path}.context_map.{raw_key}",
            )

    # Mutual exclusion with targeting fields (rooms alias into channels).
    conflicts = [
        name
        for name, value in (
            ("source_channel/source_room", rc.source_channel),
            ("dest_channel/dest_room", rc.dest_channel),
            ("dest_destination", rc.dest_destination),
        )
        if value is not None
    ]
    if conflicts:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: 'context_map' is mutually exclusive with "
            f"{conflicts}. The map supplies those fields during expansion.",
            section_path=section_path,
        )

    # The map pairs one source side with one dest side.
    if len(rc.source_adapters) != 1:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: 'context_map' requires exactly one "
            f"source adapter, got {len(rc.source_adapters)}",
            section_path=section_path,
        )
    if len(rc.dest_adapters) != 1:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: 'context_map' requires exactly one "
            f"dest adapter, got {len(rc.dest_adapters)}",
            section_path=section_path,
        )

    # Structured destinations address one specific entity; a reverse leg
    # would need inbound identity semantics they cannot provide.
    structured_keys = sorted(
        key
        for key, entry in rc.context_map.items()
        if entry.dest_destination is not None
    )
    if structured_keys and rc.directionality is not RouteDirectionality.SOURCE_TO_DEST:
        raise ConfigValidationError(
            f"Route {rc.route_id!r}: context_map entries with "
            f"'dest_destination' ({structured_keys}) require directionality "
            f"'source_to_dest'; {rc.directionality.value!r} would need "
            "inbound identity semantics a structured destination cannot "
            "provide",
            section_path=section_path,
        )

    # Ambiguous duplicate dest contexts in the directions actually enabled.
    if rc.directionality in (
        RouteDirectionality.DEST_TO_SOURCE,
        RouteDirectionality.BIDIRECTIONAL,
    ):
        seen: set[str] = set()
        dupes: set[str] = set()
        for entry in rc.context_map.values():
            if entry.dest_context is None:
                continue
            if entry.dest_context in seen:
                dupes.add(entry.dest_context)
            seen.add(entry.dest_context)
        if dupes:
            raise ConfigValidationError(
                f"Route {rc.route_id!r}: context_map has duplicate "
                f"dest_context value(s) {sorted(dupes)}, and directionality "
                f"{rc.directionality.value!r} creates reverse (dest→source) "
                "legs. Duplicate dest_context values are valid fan-in only "
                "for forward-only ('source_to_dest') routes, where each "
                "source context keeps its own forward leg; on a route with "
                "reverse legs they become duplicate inbound source contexts. "
                "Use distinct dest_context values or split the entries into "
                "separate routes.",
                section_path=section_path,
            )


# ---------------------------------------------------------------------------
# Route config
# ---------------------------------------------------------------------------

#: Destination ``kind`` values and their addressing model (routing-delivery
#: spec §2.3).  ``channel``/``matrix_room`` address a logical channel or room
#: by name; ``lxmf_destination``/``meshcore_contact`` address a specific
#: entity by hash and/or name.
_ROUTE_DESTINATION_KINDS: frozenset[str] = frozenset(
    {"channel", "lxmf_destination", "meshcore_contact", "matrix_room"}
)

#: LXMF destination hashes are 16-byte Reticulum hashes written as 32
#: hexadecimal characters — the same convention as the LXMF adapter's
#: ``propagation_node_destination`` schema field.
_LXMF_DESTINATION_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class RouteDestinationConfig:
    """Structured destination addressing for a route's delivery targets.

    Mirrors the canonical :class:`~medre.core.routing.models.RouteDestination`
    model (routing-delivery spec §2.3): identity/hash-based addressing for
    adapters whose delivery target is a specific entity rather than a named
    channel.

    Attributes
    ----------
    kind:
        Addressing scheme: ``"channel"``, ``"lxmf_destination"``,
        ``"meshcore_contact"``, or ``"matrix_room"``.
    destination_hash:
        Hash-based identifier (e.g. a 32-hex-character LXMF destination
        hash), when applicable for *kind*.
    destination_name:
        Human-readable name (channel or contact name), when applicable
        for *kind*.
    metadata:
        Extensible destination-specific parameters.
    """

    kind: str
    destination_hash: str | None = None
    destination_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls,
        route_id: str,
        data: Any,
        *,
        field_name: str,
        section_path: str,
    ) -> "RouteDestinationConfig":
        """Construct and validate from a ``routes.<id>.<field_name>`` table.

        Raises
        ------
        ConfigValidationError
            If the value is not a table, the ``kind`` is unknown, or the
            per-kind field requirements (routing-delivery §2.3) are
            violated.
        """
        if not isinstance(data, dict):
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}' must be a table "
                f"(dict), got {type(data).__name__}",
                section_path=section_path,
            )

        dest_path = f"{section_path}.{field_name}"
        raw_kind = data.get("kind")
        if not isinstance(raw_kind, str) or not raw_kind:
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}.kind' must be a "
                f"non-empty string (one of {sorted(_ROUTE_DESTINATION_KINDS)})",
                section_path=dest_path,
            )
        kind = raw_kind
        if kind not in _ROUTE_DESTINATION_KINDS:
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}.kind' {kind!r} is not a "
                f"known destination kind (one of "
                f"{sorted(_ROUTE_DESTINATION_KINDS)})",
                section_path=dest_path,
            )

        raw_hash = data.get("destination_hash")
        if raw_hash is not None and (not isinstance(raw_hash, str) or not raw_hash):
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}.destination_hash' must "
                f"be a non-empty string when set",
                section_path=dest_path,
            )
        raw_name = data.get("destination_name")
        if raw_name is not None and (not isinstance(raw_name, str) or not raw_name):
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}.destination_name' must "
                f"be a non-empty string when set",
                section_path=dest_path,
            )
        raw_metadata = data.get("metadata")
        if raw_metadata is not None and not isinstance(raw_metadata, dict):
            raise ConfigValidationError(
                f"Route {route_id!r}: '{field_name}.metadata' must be a "
                f"table (dict), got {type(raw_metadata).__name__}",
                section_path=dest_path,
            )

        unknown = set(data.keys()) - {
            "kind",
            "destination_hash",
            "destination_name",
            "metadata",
        }
        if unknown:
            raise ConfigValidationError(
                f"Route {route_id!r}: unknown key(s) "
                f"{sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))} in "
                f"{dest_path}. Accepted keys: "
                "['destination_hash', 'destination_name', 'kind', 'metadata']",
                section_path=dest_path,
            )

        destination_hash = raw_hash
        destination_name = raw_name
        metadata: dict[str, Any] = dict(raw_metadata) if raw_metadata else {}

        # Per-kind requirements (routing-delivery §2.3 kind table).
        if kind == "lxmf_destination":
            if destination_hash is None:
                raise ConfigValidationError(
                    f"Route {route_id!r}: '{field_name}.destination_hash' is "
                    f"required for kind 'lxmf_destination' and must be a "
                    f"32-hex-character LXMF destination hash",
                    section_path=dest_path,
                )
            if not _LXMF_DESTINATION_HASH_RE.fullmatch(destination_hash):
                raise ConfigValidationError(
                    f"Route {route_id!r}: '{field_name}.destination_hash' "
                    f"must be a 32-hex-character LXMF destination hash "
                    f"(16 bytes), got {destination_hash!r}",
                    section_path=dest_path,
                )
        elif kind == "meshcore_contact":
            if destination_hash is None and destination_name is None:
                raise ConfigValidationError(
                    f"Route {route_id!r}: '{field_name}' requires "
                    f"'destination_hash' or 'destination_name' for kind "
                    f"'meshcore_contact'",
                    section_path=dest_path,
                )
        else:  # "channel" / "matrix_room" address by name, never by hash.
            if destination_name is None:
                raise ConfigValidationError(
                    f"Route {route_id!r}: '{field_name}.destination_name' is "
                    f"required for kind {kind!r}",
                    section_path=dest_path,
                )
            if destination_hash is not None:
                raise ConfigValidationError(
                    f"Route {route_id!r}: '{field_name}.destination_hash' "
                    f"must not be set for kind {kind!r}; name-based "
                    f"addressing resolves the native address via the "
                    f"adapter",
                    section_path=dest_path,
                )

        return cls(
            kind=kind,
            destination_hash=destination_hash,
            destination_name=destination_name,
            metadata=metadata,
        )


# Canonical field names accepted in a ``[routes.<id>]`` section.  Every
# field that :meth:`RouteConfig.from_dict` pops must be listed here so the
# unknown-key rejection can flag typos rather than silently dropping them.
_ROUTE_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "source_adapters",
        "dest_adapters",
        "directionality",
        "enabled",
        "source_channel",
        "dest_channel",
        "source_room",
        "dest_room",
        "dest_destination",
        "source_origin_label",
        "dest_origin_label",
        "context_map",
        "policy",
        "retry",
    }
)


@dataclass(frozen=True)
class RouteConfig:
    """A single named route definition parsed from ``[routes.<id>]``.

    Attributes
    ----------
    route_id:
        Unique identifier for this route (the config section key).
    source_adapters:
        Tuple of source adapter IDs.
    dest_adapters:
        Tuple of destination adapter IDs.
    directionality:
        Direction of event flow.
    enabled:
        Whether this route is enabled at startup.
    source_channel:
        Optional source channel/conversation ID for targeting.
    dest_channel:
        Optional destination channel/conversation ID for targeting.
    source_room:
        Optional source room ID for targeting.
    dest_room:
        Optional destination room ID for targeting.
    dest_destination:
        Optional structured destination addressing for the delivery
        targets (routing-delivery spec §2.3/§2.4 identity/hash form).
        Mutually exclusive with ``dest_channel`` and ``dest_room`` — a
        route target has exactly one addressing authority — and with
        ``context_map``.  Applies to the route's configured dest side
        only; reverse expansion legs deliver to the source side, which
        carries no destination.
    policy:
        Optional static bridge policy.  ``None`` means "no restrictions".
    retry:
        Optional per-route retry policy for transient delivery failures.
        ``None`` means no retry scheduling for this route.
    context_map:
        Optional mapping of source-side opaque context strings to
        structured :class:`ContextMapEntry` values carrying exactly one
        of ``dest_context`` / ``dest_destination`` plus optional
        ``source_origin_label`` / ``dest_origin_label``. When present,
        the route is expanded at the configuration seam
        (:mod:`medre.config.route_expansion`) into one leg per mapped
        context (plus reverse legs per directionality) instead of using
        ``source_channel`` / ``dest_channel`` directly. Mutually
        exclusive with ``source_channel``, ``dest_channel``,
        ``source_room``, ``dest_room``, and route-level
        ``dest_destination``.  Requires exactly one source and one dest
        adapter.
    source_origin_label:
        Optional source-side human-readable label used for the forward
        leg of this route (source→dest).  When set, it is threaded into
        the rendering context as the source-context origin label,
        giving renderers a route-level override for relay-prefix
        attribution.  ``None`` means "unset" — renderers fall back to
        the source adapter's ``origin_label``.  This is source-context
        metadata, **not** a routing key and **not** delivery evidence.
    dest_origin_label:
        Optional source-side human-readable label used for the reverse
        leg of this route (dest→source).  Same semantics as
        ``source_origin_label`` but applied when the direction is
        swapped during expansion.  ``None`` means "unset".
    """

    route_id: str
    source_adapters: tuple[str, ...]
    dest_adapters: tuple[str, ...]
    directionality: RouteDirectionality | str = RouteDirectionality.SOURCE_TO_DEST
    enabled: bool = True
    source_channel: str | None = None
    dest_channel: str | None = None
    source_room: str | None = None
    dest_room: str | None = None
    dest_destination: RouteDestinationConfig | None = None
    policy: BridgePolicy | None = None
    retry: RouteRetryConfig | None = None
    context_map: dict[str, ContextMapEntry] | None = None
    source_origin_label: str | None = None
    dest_origin_label: str | None = None

    def __post_init__(self) -> None:
        """Normalize enum-typed fields and the ``context_map`` shape.

        ``directionality`` is coerced from its config string form so that
        programmatically constructed routes behave identically to YAML-loaded
        ones — the route engine compares enum identity, and an uncoerced
        string silently matches no expansion branch (yielding zero routes).
        ``source_room``/``dest_room`` are normalized to their
        ``*_channel`` runtime form here too (the loader used to do this
        only on the YAML path, so programmatic routes silently kept
        ``source_channel=None`` and reverse legs lost their Matrix room).
        """
        if not isinstance(self.directionality, RouteDirectionality):
            try:
                coerced = RouteDirectionality(self.directionality)
            except (TypeError, ValueError):
                valid = ", ".join(d.value for d in RouteDirectionality)
                raise ConfigValidationError(
                    f"Route {self.route_id!r}: invalid directionality "
                    f"{self.directionality!r} (valid: {valid})",
                    section_path=f"routes.{self.route_id}",
                ) from None
            object.__setattr__(self, "directionality", coerced)
        # Room/channel are aliases for the same runtime field; reject
        # conflicts, then alias room -> channel when channel is absent.
        if (
            self.source_room is not None
            and self.source_channel is not None
            and self.source_room != self.source_channel
        ):
            raise ConfigValidationError(
                f"Route {self.route_id!r}: 'source_room' ({self.source_room!r}) "
                f"and 'source_channel' ({self.source_channel!r}) are both set "
                f"but differ. Use only one — 'source_room' is an alias for "
                f"'source_channel'.",
                section_path=f"routes.{self.route_id}",
            )
        if (
            self.dest_room is not None
            and self.dest_channel is not None
            and self.dest_room != self.dest_channel
        ):
            raise ConfigValidationError(
                f"Route {self.route_id!r}: 'dest_room' ({self.dest_room!r}) "
                f"and 'dest_channel' ({self.dest_channel!r}) are both set "
                f"but differ. Use only one — 'dest_room' is an alias for "
                f"'dest_channel'.",
                section_path=f"routes.{self.route_id}",
            )
        if self.source_channel is None and self.source_room is not None:
            object.__setattr__(self, "source_channel", self.source_room)
        if self.dest_channel is None and self.dest_room is not None:
            object.__setattr__(self, "dest_channel", self.dest_room)
        if self.context_map is None:
            return
        _validate_context_map_route(self)

    @classmethod
    def from_dict(cls, route_id: str, data: dict[str, Any]) -> Self:
        """Construct from a ``[routes.<id>]`` config dict.

        Parameters
        ----------
        route_id:
            The route ID (config section key after ``routes.``).
        data:
            The parsed dict for this route.

        Raises
        ------
        ConfigValidationError
            If required fields are missing or values are invalid.
        """
        section_path = f"routes.{route_id}"

        _validate_route_id(route_id, section_path=section_path)

        data = dict(data)  # shallow copy

        # --- source_adapters (required) ---
        raw_sources = data.pop("source_adapters", None)
        if raw_sources is None:
            raise ConfigValidationError(
                f"Route {route_id!r} is missing required 'source_adapters'",
                section_path=section_path,
            )
        if not isinstance(raw_sources, list):
            raise ConfigValidationError(
                f"Route {route_id!r}: 'source_adapters' must be a list",
                section_path=section_path,
            )
        source_adapters = tuple(str(s) for s in raw_sources)
        if not source_adapters:
            raise ConfigValidationError(
                f"Route {route_id!r}: 'source_adapters' must not be empty",
                section_path=section_path,
            )

        # --- dest_adapters (required) ---
        raw_dests = data.pop("dest_adapters", None)
        if raw_dests is None:
            raise ConfigValidationError(
                f"Route {route_id!r} is missing required 'dest_adapters'",
                section_path=section_path,
            )
        if not isinstance(raw_dests, list):
            raise ConfigValidationError(
                f"Route {route_id!r}: 'dest_adapters' must be a list",
                section_path=section_path,
            )
        dest_adapters = tuple(str(d) for d in raw_dests)
        if not dest_adapters:
            raise ConfigValidationError(
                f"Route {route_id!r}: 'dest_adapters' must not be empty",
                section_path=section_path,
            )

        # --- directionality (coerced by RouteConfig.__post_init__) ---
        directionality = data.pop("directionality", "source_to_dest")

        # --- enabled ---
        enabled: bool = data.pop("enabled", True)

        # --- targeting fields ---
        source_channel: str | None = data.pop("source_channel", None)
        dest_channel: str | None = data.pop("dest_channel", None)
        source_room: str | None = data.pop("source_room", None)
        dest_room: str | None = data.pop("dest_room", None)

        # --- structured destination ---
        raw_dest_destination = data.pop("dest_destination", None)
        dest_destination: RouteDestinationConfig | None = None
        if raw_dest_destination is not None:
            dest_destination = RouteDestinationConfig.from_dict(
                route_id,
                raw_dest_destination,
                field_name="dest_destination",
                section_path=section_path,
            )
            # One addressing authority per route target (routing-delivery
            # §2.4): a structured destination replaces the channel/room
            # selector entirely.
            _dest_conflicts = [
                name
                for name, value in (
                    ("dest_channel", dest_channel),
                    ("dest_room", dest_room),
                )
                if value is not None
            ]
            if _dest_conflicts:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'dest_destination' is mutually "
                    f"exclusive with {sorted(_dest_conflicts)}. A route "
                    f"target has exactly one addressing authority: a "
                    f"structured destination or a channel selector.",
                    section_path=section_path,
                )
            if len(dest_adapters) != 1:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'dest_destination' addresses one "
                    f"transport-specific entity and requires exactly one "
                    f"dest adapter, got {len(dest_adapters)}",
                    section_path=section_path,
                )

        # Room/channel are aliases for the same runtime field.
        # Reject when both are set to different values.
        if (
            source_room is not None
            and source_channel is not None
            and source_room != source_channel
        ):
            raise ConfigValidationError(
                f"Route {route_id!r}: 'source_room' ({source_room!r}) and "
                f"'source_channel' ({source_channel!r}) are both set but "
                f"differ. Use only one — 'source_room' is an alias for "
                f"'source_channel'.",
                section_path=section_path,
            )
        if (
            dest_room is not None
            and dest_channel is not None
            and dest_room != dest_channel
        ):
            raise ConfigValidationError(
                f"Route {route_id!r}: 'dest_room' ({dest_room!r}) and "
                f"'dest_channel' ({dest_channel!r}) are both set but "
                f"differ. Use only one — 'dest_room' is an alias for "
                f"'dest_channel'.",
                section_path=section_path,
            )
        # Alias room → channel when channel is absent.
        if source_channel is None and source_room is not None:
            source_channel = source_room
        if dest_channel is None and dest_room is not None:
            dest_channel = dest_room

        # --- source_origin_label (forward leg source label) ---
        raw_source_label = data.pop("source_origin_label", None)
        source_origin_label: str | None = None
        if raw_source_label is not None:
            if isinstance(raw_source_label, bool):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'source_origin_label' must be a string, "
                    f"got {type(raw_source_label).__name__}",
                    section_path=section_path,
                )
            if not isinstance(raw_source_label, str):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'source_origin_label' must be a string, "
                    f"got {type(raw_source_label).__name__}",
                    section_path=section_path,
                )
            source_origin_label = raw_source_label

        # --- dest_origin_label (reverse leg source label) ---
        raw_dest_label = data.pop("dest_origin_label", None)
        dest_origin_label: str | None = None
        if raw_dest_label is not None:
            if isinstance(raw_dest_label, bool):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'dest_origin_label' must be a string, "
                    f"got {type(raw_dest_label).__name__}",
                    section_path=section_path,
                )
            if not isinstance(raw_dest_label, str):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'dest_origin_label' must be a string, "
                    f"got {type(raw_dest_label).__name__}",
                    section_path=section_path,
                )
            dest_origin_label = raw_dest_label

        # --- context_map ---
        raw_context_map = data.pop("context_map", None)
        context_map: dict[str, ContextMapEntry] | None = None
        if raw_context_map is not None:
            if not isinstance(raw_context_map, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'context_map' must be a table "
                    f"(dict), got {type(raw_context_map).__name__}",
                    section_path=section_path,
                )
            # Validate and normalize entries.  Route-level invariants
            # (mutual exclusion, adapter counts, directionality gating,
            # duplicate-dest-context ambiguity) are enforced by
            # ``_validate_context_map_route`` in ``__post_init__`` so that
            # parsing and direct construction share one code path.
            #
            # NOTE: duplicate *dest_context* values across entries are
            # validated there too — valid fan-in for forward-only routes,
            # rejected when reverse legs would make them ambiguous.
            normalized: dict[str, ContextMapEntry] = {}
            seen_keys: set[str] = set()
            for raw_key, raw_value in raw_context_map.items():
                key = _validate_context_map_key(
                    raw_key,
                    route_id=route_id,
                    section_path=f"{section_path}.context_map.{raw_key}",
                )
                if key in seen_keys:
                    raise ConfigValidationError(
                        f"Route {route_id!r}: context_map has duplicate "
                        f"context key {key!r}",
                        section_path=f"{section_path}.context_map.{raw_key}",
                    )
                seen_keys.add(key)

                normalized[key] = _parse_context_map_entry(
                    raw_value,
                    route_id=route_id,
                    key=key,
                    section_path=section_path,
                )
            context_map = normalized

        # --- policy ---
        raw_policy = data.pop("policy", None)
        policy: BridgePolicy | None = None
        if raw_policy is not None:
            if not isinstance(raw_policy, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'policy' must be a table",
                    section_path=section_path,
                )
            policy = BridgePolicy.from_dict(
                raw_policy,
                route_id=route_id,
                section_path=section_path,
            )
            _validate_policy(
                policy,
                route_id=route_id,
                section_path=section_path,
            )

        # --- retry ---
        raw_retry = data.pop("retry", None)
        retry: RouteRetryConfig | None = None
        if raw_retry is not None:
            if not isinstance(raw_retry, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'retry' must be a table",
                    section_path=section_path,
                )
            retry = RouteRetryConfig.from_dict(
                raw_retry,
                route_id=route_id,
                section_path=section_path,
            )

        # --- unknown key rejection ---
        # After every known field has been popped, any remaining keys are
        # typos or unsupported fields. Reject them so operators get
        # actionable feedback instead of silently losing the configuration.
        # Mirrors :meth:`BridgePolicy.from_dict` and the JSON schemas'
        # ``additionalProperties: false``.
        if data:
            unknown = sorted(data.keys(), key=lambda k: (type(k).__name__, repr(k)))
            msg = (
                f"Route {route_id!r}: unknown key(s) {unknown} in "
                f"{section_path}. Accepted keys: "
                f"{sorted(_ROUTE_KNOWN_FIELDS)}"
            )
            if "channel_room_map" in data:
                msg += (
                    ". Key 'channel_room_map' was removed in favor of "
                    "'context_map': a generic mapping of opaque source-side "
                    "contexts to entries with 'dest_context' or "
                    "'dest_destination'"
                )
            raise ConfigValidationError(msg, section_path=section_path)

        # --- self-route check ---
        sources_set = set(source_adapters)
        dests_set = set(dest_adapters)
        overlap = sources_set & dests_set
        if overlap:
            raise ConfigValidationError(
                f"Route {route_id!r}: source and destination adapters overlap: "
                f"{sorted(overlap)}. A route must not bridge an adapter "
                f"to itself.",
                section_path=section_path,
            )

        # --- duplicate targets ---
        if len(set(dest_adapters)) != len(dest_adapters):
            raise ConfigValidationError(
                f"Route {route_id!r}: duplicate entries in 'dest_adapters'",
                section_path=section_path,
            )
        if len(set(source_adapters)) != len(source_adapters):
            raise ConfigValidationError(
                f"Route {route_id!r}: duplicate entries in 'source_adapters'",
                section_path=section_path,
            )

        return cls(
            route_id=route_id,
            source_adapters=source_adapters,
            dest_adapters=dest_adapters,
            directionality=directionality,
            enabled=enabled,
            source_channel=source_channel,
            dest_channel=dest_channel,
            source_room=source_room,
            dest_room=dest_room,
            dest_destination=dest_destination,
            policy=policy,
            retry=retry,
            context_map=context_map,
            source_origin_label=source_origin_label,
            dest_origin_label=dest_origin_label,
        )


# ---------------------------------------------------------------------------
# Route collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteConfigSet:
    """Ordered, validated collection of :class:`RouteConfig` instances.

    Routes are stored in the order they appear in the config file,
    ensuring deterministic iteration.  Call :meth:`validate` after
    construction to check for duplicate IDs.

    Attributes
    ----------
    routes:
        Ordered tuple of route configurations.
    """

    routes: tuple[RouteConfig, ...] = ()

    def validate(self) -> None:
        """Validate the route set for consistency.

        Checks performed:

        * **Duplicate route IDs** — no two routes may share the same
          ``route_id``.

        Raises
        ------
        ConfigValidationError
            If a validation rule is violated.
        """
        seen: dict[str, str] = {}  # route_id → section_path
        for route in self.routes:
            if route.route_id in seen:
                raise ConfigValidationError(
                    f"Duplicate route ID {route.route_id!r} "
                    f"(first defined in {seen[route.route_id]!r})",
                    section_path=f"routes.{route.route_id}",
                )
            seen[route.route_id] = f"routes.{route.route_id}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        """Parse all ``[routes.<id>]`` sections from the config root dict.

        Parameters
        ----------
        data:
            The full parsed config dict.  Looks for a top-level ``"routes"``
            key whose values are per-route tables.

        Returns
        -------
        RouteConfigSet
            Ordered, validated route set.

        Raises
        ------
        ConfigValidationError
            If any route section is invalid or IDs are duplicated.
        """
        routes_section = data.get("routes", {})
        if routes_section is None:
            routes_section = {}
        routes: list[RouteConfig] = []
        for route_id, route_table in routes_section.items():
            if not isinstance(route_table, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r} must be a config table (mapping), "
                    f"got {type(route_table).__name__}",
                    section_path=f"routes.{route_id}",
                )
            routes.append(RouteConfig.from_dict(route_id, route_table))
        route_set = cls(routes=tuple(routes))
        route_set.validate()
        return route_set
