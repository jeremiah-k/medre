"""Secret-filtering and error-sanitization utilities.

Provides:
* :func:`sanitize_for_log` — strip secret keys and coerce values for
  structured log output.
* :func:`sanitize_error` — redact tokens/passwords from error strings
  and truncate to a safe length (moved from ``medre.runtime.snapshot``).

**Invariant:** No secrets, tokens, device keys, or crypto material
ever appear in output produced by this module.

Canonical redaction surfaces
----------------------------
* :data:`REDACTED_TOKEN` — single canonical replacement marker used by
  every redaction site (logs, env provenance, support bundle, error
  strings).
* :data:`SECRET_KEY_PATTERNS` — anchor-style regex set used for
  structured-log field matching in :func:`sanitize_for_log`.  These
  are exported so other modules can run the same field-name matcher
  against non-dict payloads.
* :data:`SECRET_FIELD_TOKENS` — strict set of field-name tokens used
  by :mod:`medre.config.env` to redact env-var names whose final
  field segment looks secret-like.  Tokenized match (split on
  non-alphanumeric) so ``enabled`` does not match because it contains
  ``ble`` as a substring.
* :data:`BROAD_SECRET_TOKENS` — intentionally-broader substring set
  used by :mod:`medre.runtime.support_bundle` for config-redaction
  where over-redaction is safer than under-redaction.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

__all__ = [
    "BROAD_SECRET_TOKENS",
    "REDACTED_TOKEN",
    "SECRET_FIELD_TOKENS",
    "SECRET_KEY_PATTERNS",
    "sanitize_error",
    "sanitize_for_log",
]

# ---------------------------------------------------------------------------
# Canonical redaction marker
# ---------------------------------------------------------------------------

REDACTED_TOKEN: str = "[REDACTED]"
"""Single canonical redaction marker used by every redaction site.

Unifies the previous ``***REDACTED***`` (support bundle, env provenance),
``[REDACTED]`` (sanitize_error) and any other ad-hoc markers.  Token chosen
for grep-friendliness: searching for ``[REDACTED]`` finds every redacted
span across logs, env provenance, support bundles, and error strings.
"""

# ---------------------------------------------------------------------------
# Secret-key detection (for sanitize_for_log)
# ---------------------------------------------------------------------------

# Canonical secret-key patterns used by both the logging layer and
# medre.core.supervision.diagnostic_contract (which imports from here).
_SECRET_KEY_REGEXES: tuple[str, ...] = (
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
    # Pre-shared-key variants (WiFi PSK, WireGuard PSK, MQTT psk, etc.)
    r"^psk$",
    r"^pre_?shared_?key$",
    r"^pre_?shared_?secret$",
    # Device identifiers (Matrix device_id, Meshtastic device-id serial).
    # device_id values are session-bound secrets; redact when surfaced
    # in logs.
    r"^device_?id$",
    r"^device_?key_?id$",
)

SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in _SECRET_KEY_REGEXES
)
"""Public canonical anchor-regex secret-key patterns.

Exported so consumers (env provenance, support bundle, diagnostic
contract) can apply the exact same field-name matcher against non-dict
payloads without re-defining the list.  Ordered-list tuple, immutable.
"""

# ---------------------------------------------------------------------------
# Token-based field-name detection (for env-var and config-redaction paths)
# ---------------------------------------------------------------------------

# Strict token set used by ``medre.config.env`` to redact adapter env-var
# final-field segments.  Tokenized match splits the lowercased field name
# on non-alphanumeric runs so ``enabled`` does not match the substring
# ``ble``.  Plural forms (``keys``, ``credentials``, ``tokens``) are
# included for completeness against common field-name conventions.
SECRET_FIELD_TOKENS: frozenset[str] = frozenset(
    {
        "token",
        "secret",
        "password",
        "key",
        "auth",
        "credential",
        "ble",
        "identity",
        # Plural forms:
        "keys",
        "credentials",
        "tokens",
    }
)

# Broad secret-name substring set used by
# ``medre.runtime.support_bundle`` for config-redaction.  Intentionally
# over-broad: redacting a benign field is safe, leaking a secret is not.
# Includes access_token (whole-phrase), refresh_token, client_secret,
# bearer, pin, and private alongside the strict SECRET_FIELD_TOKENS set.
BROAD_SECRET_TOKENS: frozenset[str] = frozenset(
    SECRET_FIELD_TOKENS
    | {
        "private",
        "pin",
        "access_token",
        "refresh_token",
        "client_secret",
        "bearer",
    }
)

_SAFE_SCALAR = (bool, int, float, str, type(None))

_MAX_ERROR_DETAIL_LEN: int = 512
"""Nominal truncation limit for error strings inside snapshots.

Output may exceed this value by up to the length of the truncation
marker (``"..."``, 3 characters) when marker preservation is triggered
for strings that were originally longer than this limit but shrank
below it after redaction.
"""


def _is_secret_key(key: str) -> bool:
    """Return True if *key* matches a known secret/token pattern."""
    return any(p.search(key) for p in SECRET_KEY_PATTERNS)


def _sanitize_value(value: Any) -> Any:
    """Coerce *value* into a log-safe form."""
    if isinstance(value, str):
        return _TOKEN_RE.sub(REDACTED_TOKEN, _SDK_RE.sub("[OBJECT_REPR]", value))
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
    r"(syt_[A-Za-z0-9]+)"
    r"|(MDAx[A-Za-z0-9+/=]{20,})"
    r"|([A-Za-z0-9+/=]{40,})"
    r"|(sk-[A-Za-z0-9]{20,})"
    r"|(api[_-]?key[=:]\s*\S+)"
    r"|(access_token[=:]\s*\S+)"
    r"|(token[=:]\s*\S+)"
    r"|(password[=:]\s*\S+)"
    r"|(secret[=:]\s*\S+)"
    r"|(credentials?[=:]\s*\S+)",
    re.IGNORECASE,
)

_SDK_RE: re.Pattern[str] = re.compile(r"<[\w.]+ object at 0x[0-9a-fA-F]+>")


def sanitize_error(error: str) -> str:
    """Sanitize an error string for safe inclusion in snapshots.

    Strips likely token/secret patterns and SDK object repr strings,
    then truncates to :data:`_MAX_ERROR_DETAIL_LEN`.  When the
    original string exceeded the limit but redaction reduced the
    result below it, a ``"..."`` marker is appended so callers can
    detect truncation.  This marker may cause the output to exceed
    :data:`_MAX_ERROR_DETAIL_LEN` by up to 3 characters.
    """
    needs_truncation = len(error) > _MAX_ERROR_DETAIL_LEN
    sanitized = _TOKEN_RE.sub(REDACTED_TOKEN, error)
    sanitized = _SDK_RE.sub("[OBJECT_REPR]", sanitized)
    if len(sanitized) > _MAX_ERROR_DETAIL_LEN:
        sanitized = sanitized[: _MAX_ERROR_DETAIL_LEN - 3] + "..."
    elif needs_truncation:
        # Sanitization reduced length (e.g. full redaction of a long
        # token-like string).  Preserve the truncation marker so callers
        # can detect that the original was truncated.
        sanitized = sanitized + "..."
    return sanitized
