"""Static route and bridge-policy models for the MEDRE config layer.

This module defines the deterministic, immutable data structures that
describe named routes between adapters — the configuration-level view
consumed by the config loader (:mod:`medre.config.loader`) and later by
the runtime builder.

It is deliberately **transport-agnostic**: adapter IDs, event kinds,
channel IDs, and sender IDs are plain strings with no SDK imports.

This module is the canonical home for route config dataclasses.
:mod:`medre.runtime.route_engine` owns runtime route expansion and
topology; it imports from this module.  :mod:`medre.config` must not
import from :mod:`medre.runtime`.

Public symbols
--------------
* :class:`RouteDirectionality` — direction of flow between source/dest
* :class:`BridgePolicy` — static allowlist policy for a route
* :class:`RouteRetryConfig` — per-route retry policy for transient failures
* :class:`RouteConfig` — a single named route definition
* :class:`RouteConfigSet` — ordered, validated collection of routes
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
            raise ConfigValidationError(
                f"{_ctx}Unknown policy key(s) {sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))} in "
                f"{policy_path}. Accepted keys: "
                f"{sorted(cls._KNOWN_FIELDS)}",
                section_path=policy_path,
            )

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
# Channel-room-map entry (per-entry structured value for channel_room_map)
# ---------------------------------------------------------------------------


# Canonical field names accepted in a structured channel_room_map entry.
_CRM_ENTRY_KNOWN_KEYS: frozenset[str] = frozenset(
    {"room", "source_origin_label", "dest_origin_label"}
)


@dataclass(frozen=True, eq=False)
class ChannelRoomMapEntry:
    """A single ``channel_room_map`` entry with optional per-entry origin labels.

    Each entry maps a Meshtastic channel index to a canonical Matrix room
    ID and optionally carries ``source_origin_label`` / ``dest_origin_label``
    that override the route-level labels for the expanded legs of this
    channel only.

    Attributes
    ----------
    room:
        Canonical Matrix room ID starting with ``!``.
    source_origin_label:
        Per-entry forward-leg source label.  ``None`` means "fall back to
        the route-level ``source_origin_label``".  An explicit ``""`` means
        "suppress the adapter-level fallback for this entry's forward leg".
    dest_origin_label:
        Per-entry reverse-leg source label.  Same semantics as
        ``source_origin_label`` but applied when the direction is swapped
        during expansion.

    Equality semantics
    ------------------
    An entry whose both labels are ``None`` compares equal to its bare
    ``room`` string.  This preserves backward compatibility with callers
    that compare the normalised ``channel_room_map`` dict against a flat
    ``dict[str, str]`` (the legacy shape).  Entries with any label set
    only compare equal to another :class:`ChannelRoomMapEntry` with the
    same three fields.
    """

    room: str
    source_origin_label: str | None = None
    dest_origin_label: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, ChannelRoomMapEntry):
            return (
                self.room == other.room
                and self.source_origin_label == other.source_origin_label
                and self.dest_origin_label == other.dest_origin_label
            )
        # Backward compatibility: a label-less entry is equivalent to its
        # bare room string (the legacy ``dict[str, str]`` shape).
        if isinstance(other, str):
            if self.source_origin_label is None and self.dest_origin_label is None:
                return self.room == other
            return False
        return NotImplemented

    def __hash__(self) -> int:
        if self.source_origin_label is None and self.dest_origin_label is None:
            return hash(self.room)
        return hash((self.room, self.source_origin_label, self.dest_origin_label))


# ---------------------------------------------------------------------------
# Channel-room-map parsing helpers (extracted from RouteConfig.from_dict)
# ---------------------------------------------------------------------------


def _validate_channel_key(
    raw_key: Any,
    route_id: str,
    section_path: str,
) -> str:
    """Validate a single ``channel_room_map`` key and return its normalised form.

    Responsibilities:

    * Reject boolean keys (explicit ``bool`` check before ``int``).
    * Accept integer or string keys, normalising to a string.
    * Parse to ``int`` and validate the range 0–7.

    Does **not** check duplicates — the caller handles that via a
    ``seen_channels`` set.

    Parameters
    ----------
    raw_key:
        The raw key from the ``channel_room_map`` table.
    route_id:
        Route ID for error messages.
    section_path:
        Dot-separated config path for error messages.

    Returns
    -------
    str
        The normalised channel string (e.g. ``"0"``).

    Raises
    ------
    ConfigValidationError
        If the key is a boolean, not an integer/string, not a valid
        integer, or outside the 0–7 range.
    """
    if isinstance(raw_key, bool):
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map channel key "
            f"{raw_key!r} is a boolean, expected an integer 0–7",
            section_path=section_path,
        )
    if isinstance(raw_key, int):
        ch_str = str(raw_key)
    elif isinstance(raw_key, str):
        ch_str = raw_key
    else:
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map channel key "
            f"{raw_key!r} is not an integer or string",
            section_path=section_path,
        )
    # Must be a valid integer 0–7.
    try:
        ch_int = int(ch_str)
    except (ValueError, TypeError):
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map channel key "
            f"{raw_key!r} is not a valid integer channel",
            section_path=section_path,
        ) from None
    if ch_int < 0 or ch_int > 7:
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map channel "
            f"{ch_int!r} is out of range (must be 0–7)",
            section_path=section_path,
        )
    return str(ch_int)


def _parse_channel_room_map_entry(
    raw_value: Any,
    route_id: str,
    ch_normalized: str,
    section_path: str,
) -> tuple[str, str | None, str | None]:
    """Parse a polymorphic ``channel_room_map`` entry value.

    Supports two shapes:

    * **Bare-string shape** — ``raw_value`` is a ``str``: the room ID only,
      no labels.  Returns ``(raw_value, None, None)``.
    * **Structured shape** — ``raw_value`` is a ``dict``: a table with
      ``room`` plus optional ``source_origin_label`` /
      ``dest_origin_label``.  Unknown keys are rejected.  Both labels use
      the bool-before-str check pattern (spec §17.5.8 requires it).

    Any other type raises :class:`ConfigValidationError`.

    Parameters
    ----------
    raw_value:
        The raw value associated with the channel key.
    route_id:
        Route ID for error messages.
    ch_normalized:
        The already-normalised channel string (used in error messages and
        to build the per-entry ``section_path``).
    section_path:
        Dot-separated config path for the route (the per-entry path is
        derived as ``{section_path}.channel_room_map.{ch_normalized}``).

    Returns
    -------
    tuple[str, str | None, str | None]
        ``(room_value_raw, entry_source_label, entry_dest_label)``.  The
        room value is **not** validated here — the caller runs it through
        :func:`_validate_room_string`.

    Raises
    ------
    ConfigValidationError
        If the value is not a string or dict, contains unknown keys, is
        missing the ``room`` key, or has a label that is not a string.
    """
    entry_path = f"{section_path}.channel_room_map.{ch_normalized}"
    entry_source_label: str | None = None
    entry_dest_label: str | None = None
    if isinstance(raw_value, str):
        # Legacy bare-string shape: room ID only, no labels.
        room_value_raw = raw_value
    elif isinstance(raw_value, dict):
        # New structured shape: table with room + optional labels.
        unknown = set(raw_value.keys()) - _CRM_ENTRY_KNOWN_KEYS
        if unknown:
            raise ConfigValidationError(
                f"Route {route_id!r}: channel_room_map entry for "
                f"channel {ch_normalized!r} has unknown key(s) "
                f"{sorted(unknown, key=lambda k: (type(k).__name__, repr(k)))}. Accepted keys: "
                f"{sorted(_CRM_ENTRY_KNOWN_KEYS)}",
                section_path=entry_path,
            )
        if "room" not in raw_value:
            raise ConfigValidationError(
                f"Route {route_id!r}: channel_room_map entry for "
                f"channel {ch_normalized!r} is missing required "
                f"'room' key",
                section_path=entry_path,
            )
        room_value_raw = raw_value["room"]
        # --- per-entry source_origin_label ---
        raw_sol = raw_value.get("source_origin_label")
        if raw_sol is not None:
            if isinstance(raw_sol, bool):
                raise ConfigValidationError(
                    f"Route {route_id!r}: channel_room_map entry "
                    f"for channel {ch_normalized!r}: "
                    f"'source_origin_label' must be a string, "
                    f"got {type(raw_sol).__name__}",
                    section_path=entry_path,
                )
            if not isinstance(raw_sol, str):
                raise ConfigValidationError(
                    f"Route {route_id!r}: channel_room_map entry "
                    f"for channel {ch_normalized!r}: "
                    f"'source_origin_label' must be a string, "
                    f"got {type(raw_sol).__name__}",
                    section_path=entry_path,
                )
            entry_source_label = raw_sol
        # --- per-entry dest_origin_label ---
        raw_dol = raw_value.get("dest_origin_label")
        if raw_dol is not None:
            if isinstance(raw_dol, bool):
                raise ConfigValidationError(
                    f"Route {route_id!r}: channel_room_map entry "
                    f"for channel {ch_normalized!r}: "
                    f"'dest_origin_label' must be a string, "
                    f"got {type(raw_dol).__name__}",
                    section_path=entry_path,
                )
            if not isinstance(raw_dol, str):
                raise ConfigValidationError(
                    f"Route {route_id!r}: channel_room_map entry "
                    f"for channel {ch_normalized!r}: "
                    f"'dest_origin_label' must be a string, "
                    f"got {type(raw_dol).__name__}",
                    section_path=entry_path,
                )
            entry_dest_label = raw_dol
    else:
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map entry for "
            f"channel {ch_normalized!r} must be a non-empty string "
            f"or a table with a 'room' key, got "
            f"{type(raw_value).__name__}",
            section_path=entry_path,
        )
    return room_value_raw, entry_source_label, entry_dest_label


def _validate_room_string(
    room_value_raw: Any,
    route_id: str,
    ch_normalized: str,
    section_path: str,
) -> str:
    """Validate and normalise a ``channel_room_map`` room value.

    Responsibilities:

    * Require a non-empty string (after ``strip()``).
    * Strip surrounding whitespace.
    * Reject ``#`` room aliases.
    * Require the ``!`` canonical-room-ID prefix.

    Does **not** check duplicates — duplicate-room ambiguity is validated
    at runtime route expansion (see :mod:`medre.runtime.route_engine`),
    where adapter platforms are known and the routing direction can
    disambiguate fan-in from ambiguous Matrix→Meshtastic routing.

    Parameters
    ----------
    room_value_raw:
        The raw room value extracted from the entry (string or table).
    route_id:
        Route ID for error messages.
    ch_normalized:
        The normalised channel string (for error messages).
    section_path:
        Dot-separated config path for error messages.

    Returns
    -------
    str
        The validated, stripped room string.

    Raises
    ------
    ConfigValidationError
        If the value is not a non-empty string, is a ``#`` alias, or
        does not start with ``!``.
    """
    if not isinstance(room_value_raw, str) or not room_value_raw.strip():
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map room for "
            f"channel {ch_normalized!r} must be a non-empty "
            f"string, got {room_value_raw!r}",
            section_path=section_path,
        )
    room_value = room_value_raw.strip()
    if room_value.startswith("#"):
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map room for "
            f"channel {ch_normalized!r} is a room alias "
            f"({room_value!r}); aliases are not supported yet — "
            f"use canonical room IDs starting with '!'",
            section_path=section_path,
        )
    if not room_value.startswith("!"):
        raise ConfigValidationError(
            f"Route {route_id!r}: channel_room_map value "
            f"{room_value!r} for channel {ch_normalized!r} must "
            f"be a canonical Matrix room ID starting with '!'",
            section_path=section_path,
        )
    return room_value


# ---------------------------------------------------------------------------
# Route config
# ---------------------------------------------------------------------------


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
    filter_hooks:
        Optional tuple of static filter hook names for later ownership.
        These are identifiers only — no callable code.
    source_channel:
        Optional source channel/conversation ID for targeting.
    dest_channel:
        Optional destination channel/conversation ID for targeting.
    source_room:
        Optional source room ID for targeting.
    dest_room:
        Optional destination room ID for targeting.
    policy:
        Optional static bridge policy.  ``None`` means "no restrictions".
    retry:
        Optional per-route retry policy for transient delivery failures.
        ``None`` means no retry scheduling for this route.
    channel_room_map:
        Optional mapping of Meshtastic channel strings ("0"–"7") to
        Matrix room IDs.  Each value may be a bare room-ID string (legacy
        shape) or a structured table carrying ``room`` plus optional
        ``source_origin_label`` / ``dest_origin_label``.  After parsing,
        values are normalised to :class:`ChannelRoomMapEntry`.  When
        present, the route is expanded at runtime into per-channel legs
        instead of using ``source_channel`` / ``dest_channel`` directly.
        Mutually exclusive with ``source_channel``, ``dest_channel``,
        ``source_room``, and ``dest_room``.  Requires exactly one source
        and one dest adapter.
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
    directionality: RouteDirectionality = RouteDirectionality.SOURCE_TO_DEST
    enabled: bool = True
    filter_hooks: tuple[str, ...] = ()
    source_channel: str | None = None
    dest_channel: str | None = None
    source_room: str | None = None
    dest_room: str | None = None
    policy: BridgePolicy | None = None
    retry: RouteRetryConfig | None = None
    channel_room_map: dict[str, ChannelRoomMapEntry] | None = None
    source_origin_label: str | None = None
    dest_origin_label: str | None = None

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

        # --- directionality ---
        raw_dir = data.pop("directionality", "source_to_dest")
        try:
            directionality = RouteDirectionality(raw_dir)
        except ValueError:
            valid = ", ".join(d.value for d in RouteDirectionality)
            raise ConfigValidationError(
                f"Route {route_id!r}: invalid directionality {raw_dir!r} "
                f"(valid: {valid})",
                section_path=section_path,
            ) from None

        # --- enabled ---
        enabled: bool = data.pop("enabled", True)

        # --- filter_hooks ---
        raw_hooks = data.pop("filter_hooks", [])
        if not isinstance(raw_hooks, list):
            raise ConfigValidationError(
                f"Route {route_id!r}: 'filter_hooks' must be a list",
                section_path=section_path,
            )
        filter_hooks = tuple(str(h) for h in raw_hooks)
        if filter_hooks:
            raise ConfigValidationError(
                f"Route {route_id!r}: 'filter_hooks' are reserved and not "
                f"yet supported. Remove filter_hooks to proceed.",
                section_path=section_path,
            )

        # --- targeting fields ---
        source_channel: str | None = data.pop("source_channel", None)
        dest_channel: str | None = data.pop("dest_channel", None)
        source_room: str | None = data.pop("source_room", None)
        dest_room: str | None = data.pop("dest_room", None)

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

        # --- channel_room_map ---
        raw_crm = data.pop("channel_room_map", None)
        channel_room_map: dict[str, ChannelRoomMapEntry] | None = None
        if raw_crm is not None:
            if not isinstance(raw_crm, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'channel_room_map' must be a table "
                    f"(dict), got {type(raw_crm).__name__}",
                    section_path=section_path,
                )
            # Mutual exclusion with targeting fields.
            _crm_exclusive = {
                "source_channel": source_channel,
                "dest_channel": dest_channel,
                "source_room": source_room,
                "dest_room": dest_room,
            }
            conflicting = [k for k, v in _crm_exclusive.items() if v is not None]
            if conflicting:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'channel_room_map' is mutually "
                    f"exclusive with {conflicting}. The map supplies those "
                    f"fields during expansion.",
                    section_path=section_path,
                )
            # Require exactly one source and one dest adapter.
            if len(source_adapters) > 1:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'channel_room_map' requires exactly "
                    f"one source adapter, got {len(source_adapters)}",
                    section_path=section_path,
                )
            if len(dest_adapters) > 1:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'channel_room_map' requires exactly "
                    f"one dest adapter, got {len(dest_adapters)}",
                    section_path=section_path,
                )
            # Validate and normalize entries.
            # NOTE: duplicate *rooms* across the map are intentionally
            # permitted here — multiple Meshtastic channels may fan into
            # the same Matrix room. Ambiguity for Matrix→Meshtastic
            # routing is enforced at runtime expansion (see
            # :mod:`medre.runtime.route_engine`), where adapter platforms
            # and route directionality are known. Duplicate *channels*
            # remain rejected below.
            normalized: dict[str, ChannelRoomMapEntry] = {}
            seen_channels: set[str] = set()
            for raw_key, raw_value in raw_crm.items():
                ch_normalized = _validate_channel_key(raw_key, route_id, section_path)
                if ch_normalized in seen_channels:
                    raise ConfigValidationError(
                        f"Route {route_id!r}: channel_room_map has duplicate "
                        f"channel {ch_normalized!r}",
                        section_path=section_path,
                    )
                seen_channels.add(ch_normalized)

                room_value_raw, entry_source_label, entry_dest_label = (
                    _parse_channel_room_map_entry(
                        raw_value, route_id, ch_normalized, section_path
                    )
                )
                room_value = _validate_room_string(
                    room_value_raw, route_id, ch_normalized, section_path
                )
                normalized[ch_normalized] = ChannelRoomMapEntry(
                    room=room_value,
                    source_origin_label=entry_source_label,
                    dest_origin_label=entry_dest_label,
                )
            channel_room_map = normalized

            # Reject empty channel_room_map — at least one mapping is required.
            if not channel_room_map:
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'channel_room_map' must have at "
                    f"least one mapping",
                    section_path=section_path,
                )

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
            filter_hooks=filter_hooks,
            source_channel=source_channel,
            dest_channel=dest_channel,
            source_room=source_room,
            dest_room=dest_room,
            policy=policy,
            retry=retry,
            channel_room_map=channel_room_map,
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
