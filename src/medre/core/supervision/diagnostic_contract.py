"""Cross-adapter diagnostics normalization contract.

Provides a pure helper :func:`normalize_diagnostics` that accepts diagnostics
from any adapter shape (``dict``, dataclass, msgspec struct, or plain object)
and produces a deterministic, JSON-safe dictionary with:

* **Common outer fields** – a fixed set of 8 keys shared across adapters.
* **Transport-specific details** – adapter-specific diagnostics preserved
  verbatim under the ``"transport_specific"`` key.

The helper imports **no adapters** and changes **no adapter internals**.  It
is observational only: missing fields resolve to ``None`` / safe defaults,
never invented success.

Common outer keys
-----------------
.. list-table::
   :widths: 30 70

   * - ``connected``
     - ``bool | None`` – whether the transport reports an active connection.
   * - ``health``
     - ``str | None`` – one of
       :data:`~medre.core.supervision.health.VALID_HEALTH_STRINGS`, or ``None``.
   * - ``mode``
     - ``str | None`` – ``"fake"``, ``"live"``, or ``None`` when unknown.
   * - ``reconnecting``
     - ``bool | None`` – whether the adapter is in a reconnection cycle.
   * - ``reconnect_attempts``
     - ``int | None`` – number of reconnection attempts so far.
   * - ``last_error``
     - ``str | None`` – most recent error string, if any.
   * - ``transient_delivery_failures``
     - ``int | None`` – count of transient (retryable) delivery failures.
   * - ``permanent_delivery_failures``
     - ``int | None`` – count of permanent (non-retryable) delivery failures.

Transport-specific details
--------------------------
All input keys that are **not** part of the common set are collected under
``"transport_specific"`` as a nested ``dict``.  This preserves adapter-specific
diagnostics without flattening or dropping safe data.

Secret / unsafe filtering
-------------------------
Keys whose names match obvious secret patterns are silently dropped from the
output (both common and transport-specific sections).  See
:data:`_SECRET_KEY_PATTERNS` for the list.

Public symbols
--------------
* :func:`normalize_diagnostics` – pure normalization function.
* :data:`COMMON_DIAGNOSTIC_KEYS` – frozenset of the 8 common key names.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from medre.core.observability.sanitization import (
    _SECRET_KEY_PATTERNS,
    _is_secret_key,
)

__all__ = [
    "COMMON_DIAGNOSTIC_KEYS",
    "normalize_diagnostics",
    "sanitize_diagnostic_value",
    "sanitize_diagnostic_mapping",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COMMON_DIAGNOSTIC_KEYS: frozenset[str] = frozenset(
    {
        "connected",
        "health",
        "mode",
        "reconnecting",
        "reconnect_attempts",
        "last_error",
        "transient_delivery_failures",
        "permanent_delivery_failures",
    }
)
"""The 8 common diagnostic key names shared across all adapters."""

# Sentinel used internally; never appears in output.
_UNSET = object()

# Types considered serialization-safe (leaf values in the output dict).
_SAFE_SCALAR_TYPES = (bool, int, float, str, type(None))

# Maximum length for string values before truncation.  Prevents unbounded
# output when a diagnostic value unexpectedly contains a very long string.
_MAX_STRING_LENGTH: int = 4096

# Recursion / size bounds for sanitization.
_SANITIZE_MAX_DEPTH: int = 10
"""Maximum nesting depth for dict/list recursion.  Containers beyond this
depth are replaced with ``"<max_depth_exceeded>"``."""

_SANITIZE_MAX_MAPPING_ENTRIES: int = 128
"""Maximum number of key-value pairs retained from a single dict.  Excess
entries are silently dropped (insertion-order preserved)."""

_SANITIZE_MAX_SEQUENCE_ITEMS: int = 256
"""Maximum number of elements retained from a single sequence.  Excess
items are dropped and a ``"<truncated: N items>"`` marker is appended."""


def _sanitize_value(value: Any, _depth: int = 0) -> Any:
    """Coerce a value into a serialization-safe form.

    * ``bool`` / ``int`` / ``float`` / ``str`` / ``None`` pass through.
    * ``dict`` is recursively sanitized (bounded by
      :data:`_SANITIZE_MAX_DEPTH` and :data:`_SANITIZE_MAX_MAPPING_ENTRIES`).
    * ``list`` / ``tuple`` / ``set`` / ``frozenset`` have each element
      sanitized and become ``list`` (bounded by
      :data:`_SANITIZE_MAX_SEQUENCE_ITEMS`).
    * Everything else (including exceptions, raw SDK objects, functions,
      classes, ``bytes``) is replaced with a type-name placeholder
      like ``"<ValueError>"`` instead of a full ``repr()``.  This
      prevents accidental leakage of secret-bearing repr output.  If
      accessing the type name or building the placeholder fails, the
      value is replaced with ``"<object>"``.
    """
    if isinstance(value, _SAFE_SCALAR_TYPES):
        if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
            return value[:_MAX_STRING_LENGTH] + f"…[{len(value)} chars]"
        return value
    if _depth >= _SANITIZE_MAX_DEPTH:
        return "<max_depth_exceeded>"
    if isinstance(value, dict):
        return _sanitize_dict(value, _depth + 1)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if len(items) > _SANITIZE_MAX_SEQUENCE_ITEMS:
            excess = len(items) - _SANITIZE_MAX_SEQUENCE_ITEMS
            sanitized = [
                _sanitize_value(item, _depth + 1)
                for item in items[:_SANITIZE_MAX_SEQUENCE_ITEMS]
            ]
            sanitized.append(f"<truncated: {excess} items>")
            return sanitized
        return [_sanitize_value(item, _depth + 1) for item in items]
    # Unsupported / complex object – safe type-name placeholder.
    try:
        return f"<{type(value).__name__}>"
    except Exception:
        return "<object>"


def _sanitize_dict(d: dict[str, Any], _depth: int = 0) -> dict[str, Any]:
    """Return a copy of *d* with secret keys removed and values sanitized.

    If *d* contains more than :data:`_SANITIZE_MAX_MAPPING_ENTRIES` keys,
    excess entries are silently dropped (insertion-order preserved).
    """
    out: dict[str, Any] = {}
    entries = list(d.items())
    if len(entries) > _SANITIZE_MAX_MAPPING_ENTRIES:
        entries = entries[:_SANITIZE_MAX_MAPPING_ENTRIES]
    for key, value in entries:
        if _is_secret_key(key):
            continue
        out[key] = _sanitize_value(value, _depth)
    return out


# ---------------------------------------------------------------------------
# Public sanitizer helpers
# ---------------------------------------------------------------------------


def sanitize_diagnostic_value(value: Any) -> Any:
    """Coerce a single value into a serialization-safe form.

    This is the public wrapper around the internal value sanitizer.
    It handles:

    * ``bool`` / ``int`` / ``float`` / ``str`` / ``None`` — passed through.
    * ``dict`` — recursively sanitized via :func:`sanitize_diagnostic_mapping`.
    * ``list`` / ``tuple`` / ``set`` / ``frozenset`` — each element
      sanitized, returned as ``list``.
    * All other types — replaced with a safe type-name placeholder
      (e.g. ``"<ValueError>"``).

    String values longer than 4096 characters are truncated.

    Parameters
    ----------
    value:
        Any Python value to sanitize.

    Returns
    -------
    Any
        A JSON-safe representation of *value*.
    """
    return _sanitize_value(value)


def sanitize_diagnostic_mapping(d: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe, bounded, secret-free copy of *d*.

    Removes keys matching known secret patterns (``password``, ``api_key``,
    etc.) and recursively sanitises all values via
    :func:`sanitize_diagnostic_value`.

    Parameters
    ----------
    d:
        A dictionary to sanitize.

    Returns
    -------
    dict[str, Any]
        A new dictionary with secrets stripped and values coerced to
        JSON-safe forms.
    """
    return _sanitize_dict(d)


def _to_flat_dict(raw: Any) -> dict[str, Any]:
    """Convert *raw* diagnostics input into a flat ``dict``.

    Accepted shapes:

    * ``dict`` / ``Mapping`` – used as-is (copy).
    * dataclass – converted via :func:`dataclasses.asdict`.
    * msgspec struct – fields extracted via ``__struct_fields__`` and
      ``getattr``.
    * Any other object – public attributes (no ``_`` prefix) extracted via
      ``getattr``.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, Mapping):
        return dict(raw)
    if is_dataclass(raw) and not isinstance(raw, type):
        return asdict(raw)
    # msgspec struct detection
    struct_fields = getattr(raw, "__struct_fields__", None)
    if struct_fields is not None:
        result: dict[str, Any] = {}
        for field_name in struct_fields:
            result[field_name] = getattr(raw, field_name, None)
        return result
    # Generic object – extract public attributes
    result = {}
    for attr in dir(raw):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(raw, attr)
        except Exception:
            continue
        # Skip callables (methods, properties that raise, etc.)
        if callable(value):
            continue
        result[attr] = value
    return result


def normalize_diagnostics(
    raw: Any,
    *,
    adapter_hint: str | None = None,
    mode_hint: str | None = None,
) -> dict[str, Any]:
    """Normalize cross-adapter diagnostics into a deterministic, JSON-safe dict.

    Accepts diagnostics from any adapter shape – ``dict``, dataclass,
    ``msgspec.Struct``, or plain object with attributes – and produces a
    dictionary with:

    1. **Common outer fields** – the 8 keys in :data:`COMMON_DIAGNOSTIC_KEYS`.
    2. **``"transport_specific"``** – a nested dict containing all remaining
       adapter-specific diagnostics, preserving safe data without flattening.

    This function is **observational only**.  It does not infer authoritative
    state beyond the fields provided.  Missing common fields resolve to
    ``None`` – never invented success.

    Parameters
    ----------
    raw:
        Raw diagnostics from an adapter.  May be a ``dict``, dataclass,
        ``msgspec.Struct``, or any object with public attributes.
    adapter_hint:
        Optional adapter name for context (stored verbatim in output
        under ``"adapter"``).  Purely informational.
    mode_hint:
        Optional mode override (``"fake"``, ``"live"``).  If provided, takes
        precedence over a ``"mode"`` key in *raw*.

    Returns
    -------
    dict[str, Any]
        Deterministic dictionary suitable for JSON serialization or
        structured logging.

    Examples
    --------
    >>> normalize_diagnostics({"connected": True, "reconnecting": False})
    {'connected': True, 'health': None, 'mode': None, ...}
    """
    flat = _to_flat_dict(raw)

    # -- Extract common fields with None fallbacks --------------------------
    common: dict[str, Any] = {}
    for key in sorted(COMMON_DIAGNOSTIC_KEYS):
        common[key] = flat.get(key, _UNSET)

    # Override mode if hint provided.
    if mode_hint is not None:
        common["mode"] = mode_hint

    # Resolve any UNSET to None, then sanitize common values.
    for key in list(common):
        if common[key] is _UNSET:
            common[key] = None
        # Filter secret keys from common section (unlikely but defensive).
        if _is_secret_key(key):
            common[key] = None
        else:
            common[key] = _sanitize_value(common[key])

    # -- Collect transport-specific details ----------------------------------
    specific: dict[str, Any] = {}
    for key, value in flat.items():
        if key in COMMON_DIAGNOSTIC_KEYS:
            continue
        if _is_secret_key(key):
            continue
        specific[key] = _sanitize_value(value)

    # -- Assemble deterministic output ---------------------------------------
    result: dict[str, Any] = {}

    # Optional metadata keys first (if present).
    if adapter_hint is not None:
        result["adapter"] = adapter_hint

    # Common keys in canonical order.
    for key in sorted(COMMON_DIAGNOSTIC_KEYS):
        result[key] = common[key]

    # Transport-specific details last.
    if specific:
        result["transport_specific"] = dict(sorted(specific.items()))

    return result
