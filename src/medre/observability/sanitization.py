"""Secret-filtering and error-sanitization utilities.

Provides:
* :func:`sanitize_for_log` — strip secret keys and coerce values for
  structured log output.
* :func:`sanitize_error` — redact tokens/passwords from error strings
  and truncate to a safe length (moved from ``medre.runtime.snapshot``).

**Invariant:** No secrets, tokens, device keys, or crypto material
ever appear in output produced by this module.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = ["sanitize_for_log", "sanitize_error"]

# ---------------------------------------------------------------------------
# Secret-key detection (for sanitize_for_log)
# ---------------------------------------------------------------------------

# Patterns match the set in medre.core.runtime.diagnostic_contract
# duplicated here to avoid a circular or heavy import at the logging layer.
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
        r"^device_?key",
        r"^signing_?key",
        r"^identity_?key",
    )
)

_SAFE_SCALAR = (bool, int, float, str, type(None))

_MAX_ERROR_DETAIL_LEN: int = 512
"""Truncation limit for error strings inside snapshots."""


def _is_secret_key(key: str) -> bool:
    """Return True if *key* matches a known secret/token pattern."""
    return any(p.search(key) for p in _SECRET_KEY_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    """Coerce *value* into a log-safe form."""
    if isinstance(value, _SAFE_SCALAR):
        return value
    if isinstance(value, dict):
        return sanitize_for_log(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(v) for v in value]
    try:
        return f"<{type(value).__name__}>"
    except Exception:
        return "<object>"


def sanitize_for_log(data: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with secret keys removed and values sanitized.

    This is the public entry-point for stripping tokens/passwords/keys
    before emitting structured log records.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _is_secret_key(key):
            continue
        out[key] = _sanitize_value(value)
    return out


# ---------------------------------------------------------------------------
# Error-string sanitization (moved from medre.runtime.snapshot)
# ---------------------------------------------------------------------------

# NOTE: The third branch previously used a negative lookahead
# ``(?!(.)\3{39,})`` to skip single-character-repeated strings, but this
# caused catastrophic backtracking on long inputs.  The lookahead has been
# removed; the trade-off is that uniform-character strings 40+ chars long
# may be redacted unnecessarily — a safe default for a secret-filter.
_TOKEN_RE: re.Pattern[str] = re.compile(
    r'(syt_[A-Za-z0-9]+)'
    r'|(MDAx[A-Za-z0-9+/=]{20,})'
    r'|([A-Za-z0-9+/=]{40,})'
    r'|(sk-[A-Za-z0-9]{20,})'
    r'|(api[_-]?key[=:]\s*\S+)'
    r'|(access_token[=:]\s*\S+)'
    r'|(token[=:]\s*\S+)'
    r'|(password[=:]\s*\S+)'
    r'|(secret[=:]\s*\S+)'
    r'|(credential[=:]\s*\S+)'
)

_SDK_RE: re.Pattern[str] = re.compile(r'<[\w.]+ object at 0x[0-9a-fA-F]+>')


def sanitize_error(error: str) -> str:
    """Sanitize an error string for safe inclusion in snapshots.

    Strips likely token/secret patterns and SDK object repr strings,
    then truncates to :data:`_MAX_ERROR_DETAIL_LEN`.
    """
    sanitized = _TOKEN_RE.sub('[REDACTED]', error)
    sanitized = _SDK_RE.sub('[OBJECT_REPR]', sanitized)
    if len(sanitized) > _MAX_ERROR_DETAIL_LEN:
        sanitized = sanitized[: _MAX_ERROR_DETAIL_LEN - 3] + "..."
    return sanitized
