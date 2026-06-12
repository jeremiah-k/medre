"""Cross-transport relay attribution model, extraction, and safe prefix
formatting.

This module provides the shared foundation that adapter renderers (Matrix,
Meshtastic, MeshCore, LXMF) use to build human-readable relay prefix
strings.  It intentionally imports **no adapter packages** — all transport-
specific data arrives through the :class:`CanonicalEvent` metadata envelope
and optional config maps.

Public symbols
--------------
* :class:`RelayAttribution` — immutable, JSON-safe attribution snapshot.
* :class:`PrefixFormatterResult` — result of safe template formatting.
* :func:`extract_relay_attribution` — data-driven extraction from events.
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
    "extract_relay_attribution",
    "format_relay_prefix",
]

# ---------------------------------------------------------------------------
# Regex for template placeholder extraction: {name}
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# ---------------------------------------------------------------------------
# All known variable names (canonical + aliases)
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
        "source_display_name",
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

# Compatibility aliases — retained for backward-compatible templates only.
# **Do NOT use in new code.**  These map Meshtastic-era template names to
# the new generic canonical fields.
_COMPAT_ALIASES: dict[str, str] = {
    "from_id": "source_sender_id",  # compat: use {sender_id}
    "longname": "source_sender_label",  # compat: use {sender}
    "shortname": "source_sender_short_label",  # compat: use {sender_short}
}

_ALL_KNOWN_NAMES = (
    _CANONICAL_NAMES
    | frozenset(_PREFERRED_ALIASES.keys())
    | frozenset(_COMPAT_ALIASES.keys())
    | frozenset({"shortname5"})  # compat: derived, not a direct alias
)


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

    **Deprecated adapter-compat fields** (retained on the dataclass for
    adapter code that directly accesses or constructs with these names;
    not exposed as formatter variables):

    * ``source_long_name`` — maps to ``source_sender_label``.
    * ``source_short_name`` — maps to ``source_sender_short_label``.
    * ``source_short_name_5`` — removed from the formatter; ``shortname5``
      is derived at format time from ``source_sender_short_label`` or
      ``source_sender_id``.

    .. note::

       ``__post_init__`` propagates deprecated fields to their generic
       replacements so that adapter code constructing with old field names
       still produces correct formatter output.

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
    source_display_name:
        Best-effort human-readable display name.
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
    source_display_name: str | None = None
    source_room_or_channel: str | None = None
    source_origin_label: str | None = None
    source_native_message_id: str | None = None
    source_native_channel_id: str | None = None
    route_id: str | None = None

    # Deprecated adapter-compat fields — retained for adapter code that
    # directly accesses or constructs with these names.  Propagated to
    # their generic replacements via __post_init__.
    source_long_name: str | None = None
    source_short_name: str | None = None
    source_short_name_5: str | None = None

    def __post_init__(self) -> None:
        """Propagate deprecated adapter-compat fields to generic fields.

        When an adapter constructs a ``RelayAttribution`` using the old
        field names (``source_long_name``, ``source_short_name``), this
        copies their values to the new generic fields so that the
        formatter produces correct output without adapter changes.
        """
        if self.source_long_name is not None:
            object.__setattr__(self, "source_sender_label", self.source_long_name)
        if self.source_short_name is not None:
            object.__setattr__(
                self, "source_sender_short_label", self.source_short_name
            )


# ---------------------------------------------------------------------------
# Variable map builder
# ---------------------------------------------------------------------------


def _build_variable_map(attr: RelayAttribution) -> dict[str, str]:
    """Build a flat ``{name: str_value}`` map including aliases.

    Every value is coalesced from ``None`` to ``""`` so that string
    operations never encounter ``None``.  Derived fields (``shortname5``)
    are computed from their source fields when not explicitly set.

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

    # Compat: derive shortname5 from sender_short_label / sender_id.
    # When sender_short_label is explicitly "" (is not None), do NOT
    # fall back to sender_id — the empty value is intentional.
    if attr.source_sender_short_label is not None:
        short5 = attr.source_sender_short_label[:5]
    elif sender_id:
        short5 = sender_id[:5]
    else:
        short5 = ""

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
        "source_display_name": attr.source_display_name or "",
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

    # Add compat aliases pointing to coalesced canonical values.
    for alias, canonical in _COMPAT_ALIASES.items():
        canon[alias] = canon[canonical]

    # Compat: shortname5 is derived, not a direct alias.
    canon["shortname5"] = short5

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
# Extraction helpers
# ---------------------------------------------------------------------------

# Platform heuristics keyed on source_adapter substring.
_PLATFORM_HEURISTICS: dict[str, str] = {
    "matrix": "matrix",
    "meshtastic": "meshtastic",
    "meshcore": "meshcore",
    "lxmf": "lxmf",
}


def _guess_platform(source_adapter: str) -> str | None:
    """Guess the platform from the adapter identifier string."""
    lowered = source_adapter.lower()
    for fragment, platform in _PLATFORM_HEURISTICS.items():
        if fragment in lowered:
            return platform
    return None


def _detect_platform_from_native(native_data: dict[str, object]) -> str | None:
    """Detect the source platform by inspecting native metadata keys.

    This provides a fallback when the adapter ID does not contain a
    recognizable platform substring (e.g. ``"radio-a"``, ``"base"``).
    Each platform is identified by the presence of characteristic
    keys in the native data dict.

    Priority order:
    1. MeshCore — namespaced ``meshcore.*`` keys.
    2. Meshtastic — ``longname``, ``shortname``, ``from_id``, etc.
    3. Matrix — ``sender``, ``event_id``, ``room_id``.
    4. LXMF — ``source_hash``, ``destination_hash``.
    """
    # MeshCore: namespaced keys from MeshCoreCodec.
    meshcore_keys = {
        "meshcore.pubkey_prefix",
        "meshcore.sender_id",
        "meshcore.channel",
        "meshcore.packet_id",
    }
    if any(k in native_data for k in meshcore_keys):
        return "meshcore"

    # Meshtastic: characteristic bare keys.
    meshtastic_keys = {"longname", "shortname", "from_id", "packet_id", "channel"}
    if any(k in native_data for k in meshtastic_keys):
        return "meshtastic"

    # Matrix: characteristic keys.
    matrix_keys = {"sender", "event_id", "room_id"}
    if any(k in native_data for k in matrix_keys):
        return "matrix"

    # LXMF: characteristic keys.
    lxmf_keys = {"source_hash", "destination_hash"}
    if any(k in native_data for k in lxmf_keys):
        return "lxmf"

    return None


def _extract_localpart(mxid: str) -> str:
    """Extract the localpart from a Matrix MXID (``@user:domain``)."""
    if mxid.startswith("@"):
        rest = mxid[1:]
        colon = rest.find(":")
        if colon > 0:
            return rest[:colon]
        return rest
    return mxid


def _extract_matrix_fields(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Extract Matrix-specific fields from native metadata."""
    sender = native_data.get("sender")
    sender_str = str(sender) if sender is not None else None

    display_name = native_data.get("displayname") or native_data.get("display_name")
    display_str = str(display_name) if display_name is not None else None

    # Localpart fallback for short label.
    short_label: str | None = None
    if sender_str:
        short_label = _extract_localpart(sender_str)

    return {
        "source_sender_id": sender_str,
        "source_display_name": display_str,
        # Generic fields.
        "source_sender_label": display_str,
        "source_sender_short_label": short_label,
        # Deprecated compat fields (populated for adapter code).
        "source_long_name": display_str,
        "source_short_name": short_label,
    }


def _extract_meshtastic_fields(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Extract Meshtastic-specific fields from native metadata."""
    longname = native_data.get("longname")
    shortname = native_data.get("shortname")
    from_id = native_data.get("from_id")

    longname_str = str(longname) if longname is not None else None
    shortname_str = str(shortname) if shortname is not None else None
    from_id_str = str(from_id) if from_id is not None else None

    return {
        "source_sender_id": from_id_str,
        "source_display_name": longname_str,
        # Generic fields.
        "source_sender_label": longname_str,
        "source_sender_short_label": shortname_str,
        # Deprecated compat fields (populated for adapter code).
        "source_long_name": longname_str,
        "source_short_name": shortname_str,
    }


def _extract_meshcore_fields(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Extract MeshCore-specific fields from native metadata.

    Prefers namespaced keys produced by ``MeshCoreCodec``
    (``meshcore.pubkey_prefix``, ``meshcore.sender_id``,
    ``meshcore.channel``, ``meshcore.packet_id``).  Falls back to bare
    fixture keys (``pubkey_prefix``, ``channel_idx``) for backward
    compatibility with test fixtures and older data.
    """
    # sender_id: prefer meshcore.pubkey_prefix, then meshcore.sender_id,
    # then bare pubkey_prefix for fixture tolerance.
    sender_val = (
        native_data.get("meshcore.pubkey_prefix")
        or native_data.get("meshcore.sender_id")
        or native_data.get("pubkey_prefix")
    )
    sender_str = str(sender_val) if sender_val is not None else None

    # channel: prefer meshcore.channel, then bare channel_idx.
    _ch = native_data.get("meshcore.channel")
    channel_val = _ch if _ch is not None else native_data.get("channel_idx")
    channel_str = str(channel_val) if channel_val is not None else None

    # packet_id: prefer meshcore.packet_id.
    pkt_val = native_data.get("meshcore.packet_id")
    pkt_str = str(pkt_val) if pkt_val is not None else None

    return {
        "source_sender_id": sender_str,
        "source_native_channel_id": channel_str,
        "source_native_message_id": pkt_str,
    }


def _extract_lxmf_fields(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Extract LXMF-specific fields from native metadata."""
    source_hash = native_data.get("source_hash")

    sender_str = str(source_hash) if source_hash is not None else None

    return {
        "source_sender_id": sender_str,
    }


def _extract_platform_fields(
    platform: str | None,
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Dispatch to the platform-specific extractor."""
    if platform == "matrix":
        return _extract_matrix_fields(native_data)
    if platform == "meshtastic":
        return _extract_meshtastic_fields(native_data)
    if platform == "meshcore":
        return _extract_meshcore_fields(native_data)
    if platform == "lxmf":
        return _extract_lxmf_fields(native_data)
    return {}


def extract_relay_attribution(
    event: CanonicalEvent,
    *,
    source_platform: str | None = None,
    source_origin_label: str | None = None,
    route_id: str | None = None,
) -> RelayAttribution:
    """Extract relay attribution from a canonical event.

    Data-driven extraction that inspects ``event.metadata.native.data``
    using namespaced keys appropriate for each transport platform.
    The platform is detected from the source adapter name or may be
    supplied explicitly.

    Parameters
    ----------
    event:
        The canonical event to extract attribution from.
    source_platform:
        Override platform detection.  When ``None``, the platform is
        guessed from ``event.source_adapter``.
    source_origin_label:
        Override origin label.  When not ``None``, takes precedence over
        any value extracted from native metadata.  Typically sourced
        from the :class:`SourceAttributionConfig` registry.
    route_id:
        Route identifier, typically from the delivery pipeline.

    Returns
    -------
    RelayAttribution
        Immutable attribution snapshot with all fields resolved.
    """
    platform = source_platform or _guess_platform(event.source_adapter)

    # Native metadata for extraction and platform detection fallback.
    native_data: dict[str, object] = {}
    if event.metadata.native is not None and event.metadata.native.data:
        native_data = dict(event.metadata.native.data)

    # When adapter-ID heuristic fails, inspect native metadata keys.
    if platform is None and native_data:
        platform = _detect_platform_from_native(native_data)

    # Base fields from the event envelope.
    fields: dict[str, str | None] = {
        "source_adapter_id": event.source_adapter,
        "source_platform": platform,
        "source_transport": event.source_transport_id,
        "source_room_or_channel": event.source_channel_id,
    }

    # Native message / channel IDs from source_native_ref.
    _auth_msg_id: str | None = None
    _auth_chan_id: str | None = None
    if event.source_native_ref is not None:
        _auth_msg_id = event.source_native_ref.native_message_id
        _auth_chan_id = event.source_native_ref.native_channel_id
        fields["source_native_message_id"] = _auth_msg_id
        fields["source_native_channel_id"] = _auth_chan_id

    # Route ID from parameter or routing metadata.
    if route_id is not None:
        fields["route_id"] = route_id
    elif event.metadata.routing is not None and event.metadata.routing.route_trace:
        fields["route_id"] = event.metadata.routing.route_trace[0]

    # Platform-specific extraction from native metadata.
    platform_fields = _extract_platform_fields(platform, native_data)
    fields.update(platform_fields)

    # source_native_ref IDs are authoritative — restore them if the
    # platform extractor overwrote them with raw metadata values.
    if _auth_msg_id is not None:
        fields["source_native_message_id"] = _auth_msg_id
    if _auth_chan_id is not None:
        fields["source_native_channel_id"] = _auth_chan_id

    # Origin label: explicit parameter wins, never overwritten by
    # platform extraction.
    if source_origin_label is not None:
        fields["source_origin_label"] = source_origin_label

    return RelayAttribution(**fields)  # type: ignore[arg-type]
