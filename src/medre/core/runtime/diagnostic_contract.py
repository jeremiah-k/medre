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
       :data:`~medre.core.runtime.health.VALID_HEALTH_STRINGS`, or ``None``.
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

import re
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

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

# Key-name patterns that indicate secrets or unsafe values.
# Matches are case-insensitive.
_SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^password$",
        r"^secret",
        r"^private_?key",
        r"^access_?token",
        r"^auth_?token",
        r"^api_?key",
        r"^credentials?$",
        r"^session_?secret",
        r"^encryption_?key",
    )
)

# Sentinel used internally; never appears in output.
_UNSET = object()

# Types considered serialization-safe (leaf values in the output dict).
_SAFE_SCALAR_TYPES = (bool, int, float, str, type(None))


def _is_secret_key(key: str) -> bool:
    """Return ``True`` if *key* matches a known secret pattern."""
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    """Coerce a value into a serialization-safe form.

    * ``bool`` / ``int`` / ``float`` / ``str`` / ``None`` pass through.
    * ``dict`` is recursively sanitized.
    * ``list`` / ``tuple`` / ``set`` / ``frozenset`` have each element
      sanitized and become ``list``.
    * Everything else (including exceptions, raw SDK objects, functions,
      classes) is converted to ``str`` via ``repr()``.  If ``repr()`` raises,
      the value is replaced with ``"<unrepresentable>"``.
    """
    if isinstance(value, _SAFE_SCALAR_TYPES):
        return value
    if isinstance(value, dict):
        return _sanitize_dict(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(item) for item in value]
    # Unsupported / complex object – stringify.
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable>"


def _sanitize_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *d* with secret keys removed and values sanitized."""
    out: dict[str, Any] = {}
    for key, value in d.items():
        if _is_secret_key(key):
            continue
        out[key] = _sanitize_value(value)
    return out


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
