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
_CANONICAL_NAMES = frozenset(
    {
        "source_adapter_id",
        "source_platform",
        "source_transport",
        "source_sender_id",
        "source_display_name",
        "source_long_name",
        "source_short_name",
        "source_short_name_5",
        "source_room_or_channel",
        "source_meshnet_name",
        "source_native_message_id",
        "source_native_channel_id",
        "route_id",
    }
)

# Alias mappings: alias -> canonical name.
_ALIASES: dict[str, str] = {
    "longname": "source_long_name",
    "shortname": "source_short_name",
    "shortname5": "source_short_name_5",
    "from_id": "source_sender_id",
    "meshnet_name": "source_meshnet_name",
}

_ALL_KNOWN_NAMES = _CANONICAL_NAMES | frozenset(_ALIASES.keys())


# ---------------------------------------------------------------------------
# RelayAttribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelayAttribution:
    """Immutable, JSON-safe snapshot of cross-transport relay attribution.

    Every field is optional (``None`` where absence is meaningful).  The
    safe prefix formatter coalesces ``None`` to the empty string so that
    templates never render the literal text ``"None"``.

    Aliases (``longname``, ``shortname``, ``shortname5``, ``from_id``,
    ``meshnet_name``) are **not** stored here — they are derived by the
    formatter from the canonical ``source_*`` fields.  This avoids
    competing sources of truth.

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
    source_display_name:
        Best-effort human-readable display name.
    source_long_name:
        Long display name (Meshtastic ``longname``, Matrix display name).
    source_short_name:
        Short display name (Meshtastic ``shortname``, Matrix localpart).
    source_short_name_5:
        First 5 characters of ``source_short_name`` falling back to
        ``source_sender_id``.
    source_room_or_channel:
        Room / channel ID from the source (``event.source_channel_id``).
    source_meshnet_name:
        Mesh network name when applicable.
    source_native_message_id:
        Native message ID from the source adapter.
    source_native_channel_id:
        Native channel ID from the source adapter.
    route_id:
        Route identifier that triggered this relay, if available.
    """

    source_adapter_id: str | None = None
    source_platform: str | None = None
    source_transport: str | None = None
    source_sender_id: str | None = None
    source_display_name: str | None = None
    source_long_name: str | None = None
    source_short_name: str | None = None
    source_short_name_5: str | None = None
    source_room_or_channel: str | None = None
    source_meshnet_name: str | None = None
    source_native_message_id: str | None = None
    source_native_channel_id: str | None = None
    route_id: str | None = None


# ---------------------------------------------------------------------------
# Variable map builder
# ---------------------------------------------------------------------------


def _build_variable_map(attr: RelayAttribution) -> dict[str, str]:
    """Build a flat ``{name: str_value}`` map including aliases.

    Every value is coalesced from ``None`` to ``""`` so that string
    operations never encounter ``None``.  Derived fields (``shortname5``)
    are computed from their source fields when not explicitly set.
    """
    short_name = attr.source_short_name or ""
    sender_id = attr.source_sender_id or ""

    # Derive short_name_5 when not explicitly set: first 5 chars of
    # short_name, falling back to first 5 chars of sender_id.
    if attr.source_short_name_5:
        short5 = attr.source_short_name_5
    elif short_name:
        short5 = short_name[:5]
    elif sender_id:
        short5 = sender_id[:5]
    else:
        short5 = ""

    canon: dict[str, str] = {
        "source_adapter_id": attr.source_adapter_id or "",
        "source_platform": attr.source_platform or "",
        "source_transport": attr.source_transport or "",
        "source_sender_id": sender_id,
        "source_display_name": attr.source_display_name or "",
        "source_long_name": attr.source_long_name or "",
        "source_short_name": short_name,
        "source_short_name_5": short5 or "",
        "source_room_or_channel": attr.source_room_or_channel or "",
        "source_meshnet_name": attr.source_meshnet_name or "",
        "source_native_message_id": attr.source_native_message_id or "",
        "source_native_channel_id": attr.source_native_channel_id or "",
        "route_id": attr.route_id or "",
    }
    # Add aliases pointing to the coalesced canonical values.
    for alias, canonical in _ALIASES.items():
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
        _PLACEHOLDER_RE.findall(template)

        variables_used: list[str] = []
        missing_variables: list[str] = []
        unknown_variables: list[str] = []

        # Build substitution map for safe replacement.
        # We do a character-by-character scan to handle unmatched braces
        # correctly, but for simplicity we use the regex approach and
        # leave non-matching brace text untouched.
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

    # Localpart fallback for short name.
    short_name: str | None = None
    if sender_str:
        short_name = _extract_localpart(sender_str)

    return {
        "source_sender_id": sender_str,
        "source_display_name": display_str,
        "source_long_name": display_str,
        "source_short_name": short_name,
    }


def _extract_meshtastic_fields(
    native_data: dict[str, object],
) -> dict[str, str | None]:
    """Extract Meshtastic-specific fields from native metadata."""
    longname = native_data.get("longname")
    shortname = native_data.get("shortname")
    from_id = native_data.get("from_id")
    meshnet_name = native_data.get("meshnet_name")

    longname_str = str(longname) if longname is not None else None
    shortname_str = str(shortname) if shortname is not None else None
    from_id_str = str(from_id) if from_id is not None else None
    meshnet_str = str(meshnet_name) if meshnet_name is not None else None

    # shortname5 convention: first 5 chars of shortname, fallback to from_id.
    short5: str | None = None
    if shortname_str:
        short5 = shortname_str[:5]
    elif from_id_str:
        short5 = from_id_str[:5]

    return {
        "source_sender_id": from_id_str,
        "source_display_name": longname_str,
        "source_long_name": longname_str,
        "source_short_name": shortname_str,
        "source_short_name_5": short5,
        "source_meshnet_name": meshnet_str,
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
    channel_val = native_data.get("meshcore.channel") or native_data.get("channel_idx")
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
    source_meshnet_name: str | None = None,
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
    source_meshnet_name:
        Override meshnet name.  Useful when the config provides it but
        the event metadata does not carry it.
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

    # Meshnet name: explicit parameter wins over native data.
    if source_meshnet_name is not None:
        fields["source_meshnet_name"] = source_meshnet_name
    elif (
        "source_meshnet_name" not in fields or fields.get("source_meshnet_name") is None
    ):
        # Already populated by platform extractor if available.
        pass

    # Compute source_short_name_5 if not already set.
    if fields.get("source_short_name_5") is None:
        sn = fields.get("source_short_name")
        sid = fields.get("source_sender_id")
        if sn:
            fields["source_short_name_5"] = sn[:5]
        elif sid:
            fields["source_short_name_5"] = sid[:5]

    return RelayAttribution(**fields)  # type: ignore[arg-type]
