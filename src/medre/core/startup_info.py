"""Adapter startup-info contract.

Core-side seam that lets an adapter surface platform facts at startup
(for example a Meshtastic board name and firmware version, or a Matrix
homeserver and user id).  The seam is deliberately narrow:

* **Duck-typed discovery.**  Core looks for an optional async
  ``startup_info()`` callable on the adapter object with ``getattr``.
  Absence means the adapter has nothing to report; adapters at any pin
  keep working.  Core never imports adapter symbols.
* **Bounded, non-blocking emission.**  When an adapter transitions to
  ready during runtime assembly, core awaits ``startup_info()`` under a
  bounded timeout.  The call may never block startup: a timeout or
  failure yields no info and never fails the adapter.
* **Pure reads.**  ``startup_info()`` formats already-gathered state the
  provider holds (diagnostics, handshake data).  It **MUST NOT** perform
  new device I/O; a provider that would have to poll simply does not
  implement it.
* **Display-only durability.**  Emitted info persists as the adapter's
  last-known startup info in the adapter state directory.  The cache is
  display-only: it is never an authority for routing or delivery
  decisions.  Values read back from the cache render with a ``cached``
  marker until a fresh emission replaces them.
* **Redaction at the core render boundary.**  Identity keys and pairing
  material are never rendered; public keys and addresses render as
  prefixes only.  Core enforces this regardless of what an adapter
  returns.

The record shape is explicitly **not schema-frozen**: nothing under
``docs/schemas`` describes it, and the durable payload may change
without a coordinated release.

See ``docs/spec/adapter-startup-info.md`` for the normative contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from medre.core.observability.sanitization import (
    REDACTED_TOKEN,
    SECRET_FIELD_TOKENS,
    SECRET_KEY_PATTERNS,
    sanitize_error,
)

__all__ = [
    "AdapterStartupInfo",
    "CACHE_FILENAME",
    "DEFAULT_EMISSION_TIMEOUT_SECONDS",
    "StartupInfoField",
    "collect_startup_info",
    "read_cache_file",
    "render_startup_info_line",
    "startup_info_callable",
    "write_cache_file",
]

_logger = logging.getLogger(__name__)

DEFAULT_EMISSION_TIMEOUT_SECONDS: float = 2.0
"""Upper bound for one ``startup_info()`` await at the ready transition.

Emission runs alongside peer adapter starts, so the bound applies per
adapter.  A provider whose ``startup_info()`` never returns costs the
startup barrier at most this many seconds and yields no info.
"""

CACHE_FILENAME: str = "startup-info.json"
"""Durable last-known startup-info file inside the adapter state directory.

The payload shape is not schema-frozen and may change between releases;
readers must tolerate unknown keys and missing fields.
"""

# Prefix length for key-like and address-like rendered values.  Matches
# Generic display cap for any single rendered value, keeping CLI lines
# bounded even when a provider returns oversized free-text facts.
_MAX_VALUE_CHARS: int = 64

_TRUNCATION_SUFFIX = "…"

# Field-name tokens that mark pairing material in addition to the
# canonical sanitization token set.
_PAIRING_TOKENS: frozenset[str] = frozenset({"pairing", "psk"})

# Name tokens that explicitly mark a *public* key.  Such names are the
# one sanctioned exception to the secret-name omission rule: the design
# keeps public keys renderable as prefixes only.
_PUBLIC_KEY_NAME_TOKENS: frozenset[str] = frozenset({"public", "pub"})

# Device identifiers are operator-visible platform facts on the same
# class as board names and firmware versions: they identify hardware in
# logs and packet headers rather than authorizing anything, so they
# render in full.  The shared error-sanitization anchor that treats
# ``device_id`` as key material stays untouched for error surfaces.
_DEVICE_ID_NAMES: frozenset[str] = frozenset({"device_id", "deviceid"})

_NAME_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

# Value shapes that are never rendered in full.
# Credential-bearing URLs are omitted outright; plain endpoints render.# Credential-bearing URLs are omitted outright; plain endpoints render.
_URL_USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]+@[^\s]+")

# Terminal-display boundary: reject control characters in rendered
# values (same rationale as the native setup-instructions seam).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StartupInfoField:
    """One redacted startup fact and its display-only freshness flag.

    ``cached`` marks values that came from the durable last-known cache
    rather than a fresh emission.  It is a provenance label for
    operators only; no runtime decision may read it.
    """

    name: str
    value: str
    cached: bool = False


@dataclass(frozen=True, slots=True)
class AdapterStartupInfo:
    """Redacted, display-only startup facts for one configured adapter."""

    adapter_id: str
    transport: str
    fields: tuple[StartupInfoField, ...]
    emitted_at: str

    def with_cached_flags(self, cached: bool) -> AdapterStartupInfo:
        """Return a copy with every field's ``cached`` flag set."""
        if all(field.cached == cached for field in self.fields):
            return self
        return AdapterStartupInfo(
            adapter_id=self.adapter_id,
            transport=self.transport,
            fields=tuple(
                StartupInfoField(name=f.name, value=f.value, cached=cached)
                for f in self.fields
            ),
            emitted_at=self.emitted_at,
        )

    def to_cache_dict(self) -> dict[str, object]:
        """Return the durable payload (without display-only flags).

        The shape is not schema-frozen; writers and readers must both
        tolerate evolution.
        """
        return {
            "adapter_id": self.adapter_id,
            "transport": self.transport,
            "emitted_at": self.emitted_at,
            "fields": [
                {"name": field.name, "value": field.value} for field in self.fields
            ],
        }

    @classmethod
    def from_cache_dict(cls, data: Mapping[str, Any]) -> AdapterStartupInfo | None:
        """Parse a durable payload; return ``None`` when it is unusable.

        Values are re-redacted on read so a tampered or stale file can
        never introduce renderable key material.
        """
        adapter_id = data.get("adapter_id")
        transport = data.get("transport")
        emitted_at = data.get("emitted_at")
        raw_fields = data.get("fields")
        if not (
            isinstance(adapter_id, str)
            and isinstance(transport, str)
            and isinstance(emitted_at, str)
            and isinstance(raw_fields, list)
        ):
            return None
        fields: list[StartupInfoField] = []
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                continue
            name = raw_field.get("name")
            value = raw_field.get("value")
            if not (isinstance(name, str) and isinstance(value, str)):
                continue
            redacted = _redact_value(name, value)
            if redacted is not None:
                fields.append(StartupInfoField(name=name, value=redacted, cached=True))
        if not fields:
            return None
        return cls(
            adapter_id=adapter_id,
            transport=transport,
            fields=tuple(fields),
            emitted_at=emitted_at,
        )


# ---------------------------------------------------------------------------
# Duck-typed discovery
# ---------------------------------------------------------------------------


def startup_info_callable(provider: object) -> Callable[[], object] | None:
    """Return the provider's ``startup_info`` callable, or ``None``.

    Discovery is duck-typed with ``getattr``: a provider without the
    symbol — or with a non-callable one — simply has nothing to report.
    Core never imports adapter symbols to find this seam.
    """
    candidate = getattr(provider, "startup_info", None)
    if candidate is None or not callable(candidate):
        return None
    provider_callable: Callable[[], object] = candidate
    return provider_callable


# ---------------------------------------------------------------------------
# Core render-boundary redaction
# ---------------------------------------------------------------------------


def _field_name_is_secret(name: str) -> bool:
    """Return True when *name* marks identity keys or pairing material.

    Combines the canonical anchor-regex patterns from the shared
    sanitization module with a tokenized check so plural forms and
    compound names (``pairing_token``, ``identity_keys``) also match.
    Public-key names (``public_key``, ``pubkey``) are the sanctioned
    exception: they are retained for prefix-only rendering.  Device
    identifiers (``device_id``) are platform facts, not secrets.
    """
    lowered = name.lower()
    if lowered in _DEVICE_ID_NAMES:
        return False
    if any(pattern.search(lowered) for pattern in SECRET_KEY_PATTERNS):
        return True
    if _is_public_key_name(lowered):
        return False
    return bool(
        frozenset(_NAME_TOKEN_SPLIT.split(lowered))
        & (SECRET_FIELD_TOKENS | _PAIRING_TOKENS)
    )


def _is_public_key_name(lowered: str) -> bool:
    """Return True when the name explicitly marks a *public* key.

    Public keys are published by the underlying protocols; an explicit
    public-key name declares the value renderable in full, including a
    bypass of the credential-shape scrub below.
    """
    tokens = frozenset(_NAME_TOKEN_SPLIT.split(lowered))
    return bool(
        tokens & _PUBLIC_KEY_NAME_TOKENS and ("key" in tokens or "pubkey" in tokens)
    )


def _redact_value(name: str, value: object) -> str | None:
    """Redact one raw value for rendering; ``None`` drops the field.

    Defense-in-depth boundary applied wherever startup facts enter a
    rendered or durable surface, regardless of what the adapter
    returned:

    * secret-named and pairing-named fields are omitted entirely;
      a value that cannot be shown safely does not show at all;
    * every other field renders in full: identifiers, addresses, and
      public keys are operator facts the underlying protocols already
      publish, and a partial value cannot be copied, verified, or
      correlated;
    * non-string scalars are stringified; containers and ``None`` are
      dropped;
    * values carrying control characters are dropped;
    * credential-bearing URLs are omitted outright;
    * token-like values render as the canonical redaction marker;
    * remaining values are capped at a pure display-length limit.

    The transformation is idempotent so core can re-apply it at every
    render boundary without changing already-rendered records.
    """
    if _field_name_is_secret(name):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = value
    else:
        return None
    if not rendered:
        return None
    if _CONTROL_CHARS_RE.search(rendered):
        return None
    if _URL_USERINFO_RE.search(rendered):
        return None
    # Same canonical token scrub the error/log surfaces use; a
    # credential-shaped value under a benign name renders the marker.
    # Explicit public-key names declare their value published and skip
    # the scrub so key material renders whole.
    if not _is_public_key_name(name.lower()):
        rendered = sanitize_error(rendered)
        if REDACTED_TOKEN in rendered:
            return rendered
    if len(rendered) > _MAX_VALUE_CHARS:
        return rendered[:_MAX_VALUE_CHARS] + _TRUNCATION_SUFFIX
    return rendered


def _redacted_fields(
    raw: Mapping[str, object], *, cached: bool
) -> tuple[StartupInfoField, ...]:
    """Redact a raw mapping into renderable startup-info fields."""
    fields: list[StartupInfoField] = []
    for raw_name, raw_value in raw.items():
        if not isinstance(raw_name, str):
            continue
        value = _redact_value(raw_name, raw_value)
        if value is None:
            continue
        fields.append(StartupInfoField(name=raw_name, value=value, cached=cached))
    return tuple(fields)


# ---------------------------------------------------------------------------
# Bounded emission
# ---------------------------------------------------------------------------


async def collect_startup_info(
    adapter_id: str,
    transport: str,
    provider: object,
    *,
    timeout_seconds: float = DEFAULT_EMISSION_TIMEOUT_SECONDS,
    emitted_at: str,
) -> AdapterStartupInfo | None:
    """Await the provider's duck-typed ``startup_info()`` under a bound.

    Returns ``None`` — never raises — when the provider has no callable,
    returns nothing usable, or exceeds *timeout_seconds*.  Caller
    cancellation still propagates: only failures of the provider call
    itself are swallowed, so runtime startup remains owner of its own
    task cancellation.
    """
    provider_callable = startup_info_callable(provider)
    if provider_callable is None:
        return None
    deadline = asyncio.timeout(timeout_seconds)
    try:
        async with deadline:
            raw = provider_callable()
            if inspect.isawaitable(raw):
                raw = await raw
    except TimeoutError:
        # The bounded deadline expired: no info, and startup proceeds.
        # Caller cancellation is NOT caught — CancelledError derives from
        # BaseException on Python 3.11+ and must keep propagating.
        return None
    except Exception as exc:
        _logger.debug(
            "Adapter %s.%s startup_info unavailable (%s)",
            transport,
            adapter_id,
            type(exc).__name__,
        )
        return None
    if not isinstance(raw, Mapping):
        return None
    fields = _redacted_fields(raw, cached=False)
    if not fields:
        return None
    return AdapterStartupInfo(
        adapter_id=adapter_id,
        transport=transport,
        fields=fields,
        emitted_at=emitted_at,
    )


# ---------------------------------------------------------------------------
# Durable last-known cache
# ---------------------------------------------------------------------------


def write_cache_file(path: Path, info: AdapterStartupInfo) -> None:
    """Persist one adapter's last-known startup info atomically.

    Creates parent directories.  Raises on I/O failure; callers treat
    the cache as best-effort and must not let a write failure affect
    startup or delivery.
    """
    payload = info.to_cache_dict()
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(encoded, encoding="utf-8")
    os.replace(tmp_path, path)


def read_cache_file(path: Path) -> AdapterStartupInfo | None:
    """Load one adapter's last-known startup info, if any.

    Every field of a cache read is marked ``cached``: values persisted
    by an earlier emission are last-known facts, not fresh ones.  Any
    read or parse failure yields ``None`` — the cache is display-only
    and its absence is normal (first boot, cleared state, malformed
    payload).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    info = AdapterStartupInfo.from_cache_dict(raw)
    if info is None:
        return None
    return info.with_cached_flags(True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_startup_info_line(info: AdapterStartupInfo) -> str:
    """Render the per-adapter startup-info line for human CLI surfaces.

    Follows the CLI provenance grammar: values render as ``key=value``
    pairs and a cached record carries the ``(cached)`` marker as a
    parenthesized provenance suffix.  Values are re-redacted here so the
    boundary holds even for records that never passed through this
    module's emission path.
    """
    parts: list[str] = []
    for field in info.fields:
        value = _redact_value(field.name, field.value)
        if value is None:
            continue
        parts.append(f"{field.name}={value}")
    if not parts:
        return ""
    line = "startup: " + " ".join(parts)
    if all(field.cached for field in info.fields):
        line += " (cached)"
    return line
