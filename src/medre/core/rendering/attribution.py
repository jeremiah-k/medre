"""Cross-transport relay attribution model, formatter, and generic builder.

This module provides the shared foundation that adapter renderers (Matrix,
Meshtastic, MeshCore, LXMF) use to build human-readable relay prefix
strings.  It intentionally imports **no adapter packages** — all transport-
specific data arrives through adapter projection helpers and is passed to
the generic builder as pre-projected fields.

Public symbols
--------------
* :class:`RelayAttribution` — immutable, JSON-safe attribution snapshot.
* :class:`PrefixFormatterResult` — result of safe template formatting.
* :func:`build_relay_attribution` — generic builder from event envelope
  fields and pre-projected adapter fields.
* :func:`format_relay_prefix` — safe, never-raises template formatter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from medre.core.events import CanonicalEvent

__all__ = [
    "PrefixFormatterResult",
    "RelayAttribution",
    "build_relay_attribution",
    "format_relay_prefix",
]

# ---------------------------------------------------------------------------
# Regex for template placeholder extraction: {name}
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# ---------------------------------------------------------------------------
# All known variable names (canonical + preferred aliases)
# ---------------------------------------------------------------------------

# Canonical field names (as they appear on RelayAttribution).
# Generic, platform-neutral naming — no transport-specific terminology.
_CANONICAL_NAMES = frozenset(
    {
        "source_adapter_id",
        "source_platform",
        "source_transport",
        "source_sender_id",
        "source_sender_label",
        "source_sender_short_label",
        "source_sender_handle",
        "source_room_or_channel",
        "source_origin_label",
        "source_native_message_id",
        "source_native_channel_id",
        "route_id",
    }
)

# Preferred formatter aliases — generic, platform-neutral short names
# for use in new templates.
_PREFERRED_ALIASES: dict[str, str] = {
    "sender": "source_sender_label",
    "sender_short": "source_sender_short_label",
    "sender_id": "source_sender_id",
    "sender_handle": "source_sender_handle",
    "platform": "source_platform",
    "route_id": "route_id",
    "channel": "source_room_or_channel",
    "origin_label": "source_origin_label",
}

_ALL_KNOWN_NAMES = _CANONICAL_NAMES | frozenset(_PREFERRED_ALIASES.keys())


# ---------------------------------------------------------------------------
# RelayAttribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayAttribution:
    """Immutable, JSON-safe snapshot of cross-transport relay attribution.

    Every field is optional (``None`` where absence is meaningful).  The
    safe prefix formatter coalesces ``None`` to the empty string so that
    templates never render the literal text ``"None"``.

    **Canonical generic fields** (use these in new code):

    * ``source_sender_label`` — primary human-readable sender label.
    * ``source_sender_short_label`` — abbreviated sender label.
    * ``source_sender_handle`` — sender handle / address.
    * ``source_origin_label`` — human-readable origin label.

    Attributes
    ----------
    source_adapter_id:
        Adapter instance ID that produced the event
        (``event.source_adapter``).
    source_platform:
        Platform name (``"matrix"``, ``"meshtastic"``, ``"meshcore"``,
        ``"lxmf"``) or ``None`` when unknown.
    source_transport:
        Transport identifier (``event.source_transport_id``).
    source_sender_id:
        Native sender identifier (MXID, node ID, pubkey prefix, hash).
    source_sender_label:
        Primary human-readable sender label (display name, long name, etc.).
    source_sender_short_label:
        Abbreviated sender label (localpart, short name, etc.).
    source_sender_handle:
        Sender handle or address (Matrix handle, callsign, etc.).
    source_room_or_channel:
        Room / channel ID from the source (``event.source_channel_id``).
    source_origin_label:
        Human-readable label for the source origin (e.g. ``"East
        Meshtastic"``).  Resolved through the ``origin_label`` alias
        in prefix templates.
    source_native_message_id:
        Native message ID from the source adapter.
    source_native_channel_id:
        Native channel ID from the source adapter.
    route_id:
        Route identifier that triggered this relay, if available.
    """

    # Generic canonical fields (platform-neutral).
    source_adapter_id: str | None = None
    source_platform: str | None = None
    source_transport: str | None = None
    source_sender_id: str | None = None
    source_sender_label: str | None = None
    source_sender_short_label: str | None = None
    source_sender_handle: str | None = None
    source_room_or_channel: str | None = None
    source_origin_label: str | None = None
    source_native_message_id: str | None = None
    source_native_channel_id: str | None = None
    route_id: str | None = None


# ---------------------------------------------------------------------------
# Variable map builder
# ---------------------------------------------------------------------------


def _build_variable_map(attr: RelayAttribution) -> dict[str, str]:
    """Build a flat ``{name: str_value}`` map including aliases.

    Every value is coalesced from ``None`` to ``""`` so that string
    operations never encounter ``None``.

    Uses ``is not None`` checks for label fields to preserve explicitly
    empty strings without fallback.
    """
    sender_id = attr.source_sender_id or ""

    # Use is-not-None to preserve explicit empty strings.
    sender_label = (
        attr.source_sender_label if attr.source_sender_label is not None else ""
    )
    sender_short_label = (
        attr.source_sender_short_label
        if attr.source_sender_short_label is not None
        else ""
    )

    canon: dict[str, str] = {
        "source_adapter_id": attr.source_adapter_id or "",
        "source_platform": attr.source_platform or "",
        "source_transport": attr.source_transport or "",
        "source_sender_id": sender_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
        "source_sender_handle": (
            attr.source_sender_handle if attr.source_sender_handle is not None else ""
        ),
        "source_room_or_channel": attr.source_room_or_channel or "",
        "source_origin_label": (
            attr.source_origin_label if attr.source_origin_label is not None else ""
        ),
        "source_native_message_id": attr.source_native_message_id or "",
        "source_native_channel_id": attr.source_native_channel_id or "",
        "route_id": attr.route_id or "",
    }

    # Add preferred aliases pointing to coalesced canonical values.
    for alias, canonical in _PREFERRED_ALIASES.items():
        canon[alias] = canon[canonical]

    return canon


# ---------------------------------------------------------------------------
# PrefixFormatterResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefixFormatterResult:
    """Immutable result of safe relay-prefix template formatting.

    Attributes
    ----------
    rendered_prefix:
        The rendered string.  Never ``None``; at minimum the empty string.
    template_used:
        The original template string passed to the formatter.
    variables_used:
        Tuple of variable names that appeared in the template **and**
        were found in the variable map (value resolved, even if empty).
    missing_variables:
        Tuple of variable names that appeared in the template and are
        part of the known schema but whose value was ``None`` / empty
        on the attribution object.
    unknown_variables:
        Tuple of variable names that appeared in the template but are
        not part of the known schema at all.
    formatting_error:
        ``None`` when formatting succeeded without issues.  Set to a
        descriptive string when unknown placeholders are encountered or
        when the formatter caught an internal error.
    """

    rendered_prefix: str
    template_used: str
    variables_used: tuple[str, ...]
    missing_variables: tuple[str, ...]
    unknown_variables: tuple[str, ...]
    formatting_error: str | None


# ---------------------------------------------------------------------------
# Safe prefix formatter
# ---------------------------------------------------------------------------


def format_relay_prefix(
    template: str,
    attr: RelayAttribution,
) -> PrefixFormatterResult:
    """Render a relay prefix template against attribution data.

    **Safety guarantees:**

    * Never raises — all internal errors are captured in
      ``formatting_error``.
    * Never renders ``None`` as the literal string ``"None"``.
    * Handles unmatched braces safely (passes them through unchanged).
    * Deterministic: same inputs always produce the same output.

    **Unknown-placeholder policy:**

    Placeholders that are not part of the known variable schema are left
    unchanged in the output (e.g. ``"{bogus}"`` stays ``"{bogus}"``) and
    recorded in ``unknown_variables`` with ``formatting_error`` set.

    Parameters
    ----------
    template:
        Template string with ``{name}`` placeholders.
    attr:
        Attribution data to fill into the template.

    Returns
    -------
    PrefixFormatterResult
        Frozen result with rendered string and diagnostic metadata.
    """
    try:
        var_map = _build_variable_map(attr)

        variables_used: list[str] = []
        missing_variables: list[str] = []
        unknown_variables: list[str] = []

        def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
            name = m.group(1)
            if name in _ALL_KNOWN_NAMES:
                value = var_map.get(name, "")
                variables_used.append(name)
                if not value:
                    missing_variables.append(name)
                return value
            else:
                unknown_variables.append(name)
                return m.group(0)  # leave unchanged

        rendered = _PLACEHOLDER_RE.sub(_replace, template)

        formatting_error: str | None = None
        if unknown_variables:
            formatting_error = (
                f"unknown placeholder(s): {', '.join(sorted(unknown_variables))}"
            )

        return PrefixFormatterResult(
            rendered_prefix=rendered,
            template_used=template,
            variables_used=tuple(variables_used),
            missing_variables=tuple(missing_variables),
            unknown_variables=tuple(unknown_variables),
            formatting_error=formatting_error,
        )
    except Exception as exc:
        # Catch-all: never propagate exceptions from formatting.
        return PrefixFormatterResult(
            rendered_prefix=template,
            template_used=template,
            variables_used=(),
            missing_variables=(),
            unknown_variables=(),
            formatting_error=f"formatting_exception: {exc}",
        )


# ---------------------------------------------------------------------------
# Generic builder
# ---------------------------------------------------------------------------


def build_relay_attribution(
    event: CanonicalEvent,
    *,
    source_origin_label: str | None = None,
    route_id: str | None = None,
    projected_fields: dict[str, str | None] | None = None,
) -> RelayAttribution:
    """Build relay attribution from generic event envelope fields and
    pre-projected adapter fields.

    This function copies generic envelope fields (adapter ID, transport,
    channel, native refs, route trace) and merges pre-projected source
    sender fields.  It does **not** inspect native transport keys —
    adapter renderers must call their adapter's projection helper first
    and pass the result via *projected_fields*.

    **Merge precedence:**

    1. Generic envelope fields from the event.
    2. Projected adapter fields (overwrite envelope defaults for sender
       identity fields).
    3. ``source_native_ref`` IDs are authoritative and restore any
       values that projection may have overwritten.
    4. *route_id* parameter or routing metadata.
    5. *source_origin_label* parameter.

    Parameters
    ----------
    event:
        The canonical event to extract envelope fields from.
    source_origin_label:
        Human-readable origin label.  When not ``None``, takes precedence
        over any value in *projected_fields*.  Typically sourced from the
        ``RenderingContext`` or source attribution config registry.
    route_id:
        Route identifier.  When not ``None``, takes precedence over
        ``event.metadata.routing.route_trace``.
    projected_fields:
        Pre-projected generic attribution fields from an adapter
        projection helper.  Keys must match ``RelayAttribution`` field
        names.  Typically the return value of
        :func:`~medre.adapters._attribution_dispatch.project_source_fields`
        or an adapter-specific projection helper.

    Returns
    -------
    RelayAttribution
        Immutable attribution snapshot with all fields resolved.
    """
    fields: dict[str, str | None] = {
        "source_adapter_id": event.source_adapter,
        "source_transport": event.source_transport_id,
        "source_room_or_channel": event.source_channel_id,
    }

    # Merge projected adapter fields (sender identity, platform, etc.).
    if projected_fields:
        fields.update(projected_fields)

    # source_native_ref IDs are authoritative — restore them if the
    # projection helper overwrote them with raw metadata values.
    if event.source_native_ref is not None:
        _msg_id = event.source_native_ref.native_message_id
        _chan_id = event.source_native_ref.native_channel_id
        if _msg_id is not None:
            fields["source_native_message_id"] = _msg_id
        if _chan_id is not None:
            fields["source_native_channel_id"] = _chan_id

    # Route ID from parameter or routing metadata.
    if route_id is not None:
        fields["route_id"] = route_id
    elif event.metadata.routing is not None and event.metadata.routing.route_trace:
        fields["route_id"] = event.metadata.routing.route_trace[0]

    # Origin label: explicit parameter wins, never overwritten by
    # projection.
    if source_origin_label is not None:
        fields["source_origin_label"] = source_origin_label

    return RelayAttribution(**fields)  # type: ignore[arg-type]
