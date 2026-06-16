"""Configuration error hierarchy for MEDRE runtime configuration.

All configuration-related errors inherit from :class:`ConfigError` so
callers can catch the base class or a specific subclass as needed.
"""

from collections.abc import Iterable


class ConfigError(Exception):
    """Base exception for all configuration errors."""


class ConfigNotFoundError(ConfigError):
    """Raised when the configuration file cannot be found."""


class ConfigValidationError(ConfigError):
    """Raised when configuration validation fails.

    Parameters
    ----------
    message:
        Human-readable description of the problem.
    transport:
        Transport type involved (e.g. ``"matrix"``), if applicable.
    adapter_id:
        Adapter identifier involved, if applicable.
    section_path:
        Dot-separated config path like ``"adapters.matrix.main"``, if applicable.
    """

    def __init__(
        self,
        message: str = "",
        *,
        transport: str | None = None,
        adapter_id: str | None = None,
        section_path: str | None = None,
    ) -> None:
        self.transport = transport
        self.adapter_id = adapter_id
        self.section_path = section_path
        super().__init__(message)


class ConfigFileError(ConfigError):
    """Raised when the configuration file cannot be read or parsed."""


# ---------------------------------------------------------------------------
# Migration diagnostics for removed keys (F-018)
# ---------------------------------------------------------------------------
# When operators migrate from an older MEDRE config, they may still use keys
# that were removed or renamed by prior changes (e.g. ``meshnet_name`` →
# ``source_origin_label`` / ``dest_origin_label``). The unknown-key rejection
# surfaces the offending key name; this mapping lets the rejection also point
# at the current replacement so the operator can fix the config without
# reading source code or change fragments.
#
# Value-safety: every suggestion references only key NAMES and replacement
# field names — never operator-supplied values — so appending these hints to
# an error message cannot leak secrets (see audit F-010..F-013).
_REMOVED_KEY_SUGGESTIONS: dict[str, str] = {
    "meshnet_name": "removed; use origin_label / source_origin_label / dest_origin_label",
    "matrix_relay_prefix": (
        "removed from MeshtasticConfig; use MatrixConfig.relay_prefix "
        "on the Matrix adapter"
    ),
    "longname": "removed as a generic field; use sender or sender_short in renderer templates",
    "shortname": "removed as a generic field; use sender or sender_short in renderer templates",
    "shortname5": "removed as a generic field; use sender_short in renderer templates",
    "from_id": (
        "removed as a generic field; use sender_id or sender_handle in renderer templates"
    ),
}


def suggest_removed_key(key: str) -> str | None:
    """Return a migration suggestion for a removed *key*, or ``None``.

    The suggestion is value-free: it names the replacement field(s) only,
    so it is safe to append to error messages that may be logged or shown
    to operators.
    """
    return _REMOVED_KEY_SUGGESTIONS.get(key)


def format_removed_key_hints(keys: Iterable[str]) -> str:
    """Build a value-free hint block for removed keys among *keys*.

    Returns ``""`` when none of *keys* match a known removed key, so
    callers can unconditionally append the result to an error message
    without conditioning on whether a hint exists.

    The returned block (when non-empty) starts with a newline so it
    appends cleanly to single-line error messages::

        msg = f"unknown key(s) {sorted(unknown)}. Accepted keys: {sorted(known)}"
        msg += format_removed_key_hints(unknown)

    Suggestions are deterministic: keys are sorted, and each appears at
    most once.
    """
    suggestions = [
        f"{k}: {hint}"
        for k in sorted(k for k in keys if isinstance(k, str))
        if (hint := suggest_removed_key(k)) is not None
    ]
    if not suggestions:
        return ""
    return "\n  Hints:\n    " + "\n    ".join(suggestions)
