"""Static route and bridge-policy models for the MEDRE runtime.

This module defines the deterministic, immutable data structures that
describe named routes between adapters — the configuration-level view
consumed by the TOML loader (:mod:`medre.config.loader`) and later by
the runtime builder.

It is deliberately **transport-agnostic**: adapter IDs, event kinds,
channel IDs, and sender IDs are plain strings with no SDK imports.

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
from typing import Any, Self

from medre.config.errors import ConfigValidationError

# ---------------------------------------------------------------------------
# Directionality enum
# ---------------------------------------------------------------------------


class RouteDirectionality(Enum):
    """Direction of event flow between source and destination adapters.

    Values correspond to the ``directionality`` TOML key in
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
        Event kinds this policy permits (e.g. ``("message",)``).
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

    @classmethod
    def from_toml_dict(cls, data: dict[str, Any]) -> Self:
        """Construct from a TOML table dict (the ``[routes.<id>.policy]`` section)."""
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


def _reject_unsupported_policy_fields(
    policy: BridgePolicy,
    *,
    route_id: str,
    section_path: str,
) -> None:
    """Raise :class:`ConfigValidationError` for policy fields not enforced at runtime.

    Only ``allowed_event_types`` is currently supported — it maps to
    :attr:`RouteSource.event_kinds` during expansion.  All other policy
    fields are reserved placeholders that silently no-op; rejecting them
    prevents operators from being misled about what is enforced.
    """
    unsupported: list[str] = []
    if policy.sender_allowlist:
        unsupported.append("sender_allowlist")
    if policy.allowed_source_adapters:
        unsupported.append("allowed_source_adapters")
    if policy.allowed_dest_adapters:
        unsupported.append("allowed_dest_adapters")
    if policy.room_allowlist:
        unsupported.append("room_allowlist")
    if policy.channel_allowlist:
        unsupported.append("channel_allowlist")
    if unsupported:
        raise ConfigValidationError(
            f"Route {route_id!r}: policy fields {unsupported} are reserved "
            f"and not yet supported at runtime. Remove them to proceed.",
            section_path=f"{section_path}.policy",
        )


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
    def from_toml_dict(
        cls,
        data: dict[str, Any],
        *,
        route_id: str,
        section_path: str,
    ) -> Self:
        """Construct from a ``[routes.<id>.retry]`` TOML table dict.

        Parameters
        ----------
        data:
            The parsed TOML table for the retry section.
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
# Route config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteConfig:
    """A single named route definition parsed from ``[routes.<id>]``.

    Attributes
    ----------
    route_id:
        Unique identifier for this route (the TOML section key).
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
        Matrix room IDs.  When present, the route is expanded at runtime
        into per-channel legs instead of using ``source_channel`` /
        ``dest_channel`` directly.  Mutually exclusive with
        ``source_channel``, ``dest_channel``, ``source_room``, and
        ``dest_room``.  Requires exactly one source and one dest adapter.
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
    channel_room_map: dict[str, str] | None = None

    @classmethod
    def from_toml_dict(cls, route_id: str, data: dict[str, Any]) -> Self:
        """Construct from a ``[routes.<id>]`` TOML table dict.

        Parameters
        ----------
        route_id:
            The route ID (TOML section key after ``routes.``).
        data:
            The parsed TOML table for this route.

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

        # --- channel_room_map ---
        raw_crm = data.pop("channel_room_map", None)
        channel_room_map: dict[str, str] | None = None
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
            normalized: dict[str, str] = {}
            seen_channels: set[str] = set()
            seen_rooms: set[str] = set()
            for raw_key, raw_value in raw_crm.items():
                # --- channel key validation ---
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
                ch_normalized = str(ch_int)
                if ch_normalized in seen_channels:
                    raise ConfigValidationError(
                        f"Route {route_id!r}: channel_room_map has duplicate "
                        f"channel {ch_normalized!r}",
                        section_path=section_path,
                    )
                seen_channels.add(ch_normalized)

                # --- room value validation ---
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise ConfigValidationError(
                        f"Route {route_id!r}: channel_room_map room for "
                        f"channel {ch_normalized!r} must be a non-empty "
                        f"string, got {raw_value!r}",
                        section_path=section_path,
                    )
                room_value = raw_value.strip()
                if room_value.startswith("#"):
                    raise ConfigValidationError(
                        f"Route {route_id!r}: channel_room_map room "
                        f"{room_value!r} for channel {ch_normalized!r} "
                        f"is an alias — aliases are not supported yet; "
                        f"use a canonical room ID starting with '!'",
                        section_path=section_path,
                    )
                if room_value in seen_rooms:
                    raise ConfigValidationError(
                        f"Route {route_id!r}: channel_room_map has duplicate "
                        f"room {room_value!r}",
                        section_path=section_path,
                    )
                seen_rooms.add(room_value)
                normalized[ch_normalized] = room_value
            channel_room_map = normalized

        # --- policy ---
        raw_policy = data.pop("policy", None)
        policy: BridgePolicy | None = None
        if raw_policy is not None:
            if not isinstance(raw_policy, dict):
                raise ConfigValidationError(
                    f"Route {route_id!r}: 'policy' must be a table",
                    section_path=section_path,
                )
            policy = BridgePolicy.from_toml_dict(raw_policy)
            _reject_unsupported_policy_fields(
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
            retry = RouteRetryConfig.from_toml_dict(
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
        )


# ---------------------------------------------------------------------------
# Route collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteConfigSet:
    """Ordered, validated collection of :class:`RouteConfig` instances.

    Routes are stored in the order they appear in the TOML file,
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
    def from_toml_dict(cls, data: dict[str, Any]) -> Self:
        """Parse all ``[routes.<id>]`` sections from the TOML root dict.

        Parameters
        ----------
        data:
            The full parsed TOML dict.  Looks for a top-level ``"routes"``
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
                    f"Route {route_id!r} must be a TOML table, "
                    f"got {type(route_table).__name__}",
                    section_path=f"routes.{route_id}",
                )
            routes.append(RouteConfig.from_toml_dict(route_id, route_table))
        route_set = cls(routes=tuple(routes))
        route_set.validate()
        return route_set
