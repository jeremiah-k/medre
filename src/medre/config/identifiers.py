r"""Authoritative contract for configured adapter identifiers.

An *adapter identifier* is the configured identity of one adapter instance:
the ``adapters.<transport>.<instance_name>`` mapping key, the explicit
``adapter_id`` value that overrides it, the ``ADAPTER_ID`` value supplied to
environment-first adapter creation, and the environment token derived from
it.  Because the identifier becomes a single filesystem component under
``{state}/adapters/`` *and* an environment-variable token, one contract
governs all of those seams.

This module is deliberately narrow.  It constrains only **configured
adapter identifiers**:

* Route IDs keep their own contract (``medre.config.routes``).
* Transport names are supported-kind values, not adapter identifiers.
* ``origin_label`` and other display fields remain free prose.
* Native transport identities (Matrix MXIDs and room IDs, MeshCore node
  numbers and public-key prefixes, hashes, ...) never pass through this
  restriction.

The rule is conservative and host-independent on purpose:

* The first character must be a letter or digit; the rest may be letters,
  digits, dots, hyphens, and underscores.
* Separators (``/`` and ``\\``), NUL, whitespace, empty values, and
  dot-only segments (``.``, ``..``, ``...``) can never match, on any
  host — no reliance on ``os.sep``/``os.altsep``.
* Drive-letter-like components (``C:``), Windows DOS device basenames
  (``CON``, ``NUL``, ``COM1`` …, including extensions), trailing periods,
  and other path-ambiguous text are rejected on every host.
* Every accepted identifier contains at least one alphanumeric character,
  so the environment-token derivation in :func:`medre.config.env.normalize_adapter_id`
  can never produce an empty token from an accepted identifier.
* Length is capped at :data:`ADAPTER_ID_MAX_LENGTH` so identifier-derived
  directories fail validation instead of failing ``mkdir`` late with
  ``ENAMETOOLONG``.

Identifiers are never silently stripped, renamed, or folded together: two
distinct configured values never collapse into one state directory, and
values that would collide on their environment tokens are rejected by the
caller (see :meth:`medre.config.model.AdapterConfigSet.validate`).
"""

from __future__ import annotations

import re

__all__ = [
    "ADAPTER_ID_MAX_LENGTH",
    "ADAPTER_ID_PATTERN_SOURCE",
    "adapter_id_problem",
]

#: Longest accepted adapter identifier, in characters.  Identifiers become a
#: single directory component under ``{state}/adapters/``; 255 is the
#: smallest component-name limit of the supported hosts (POSIX ``NAME_MAX``).
ADAPTER_ID_MAX_LENGTH = 255

#: Portable identifier grammar (see module docstring).  Exposed so schemas
#: and documentation can reference the single authoritative source pattern.
#: Consumers should apply it anchored / with :func:`re.fullmatch`.
ADAPTER_ID_PATTERN_SOURCE = (
    r"(?!(?:[Cc][Oo][Nn]|[Pp][Rr][Nn]|[Aa][Uu][Xx]|[Nn][Uu][Ll]|"
    r"[Cc][Oo][Mm][1-9]|[Ll][Pp][Tt][1-9])(?:[.]|$))"
    r"(?!.*[.]$)[A-Za-z0-9][A-Za-z0-9._-]*"
)

_ADAPTER_ID_RE = re.compile(ADAPTER_ID_PATTERN_SOURCE)
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:[.]|$)", re.IGNORECASE
)

_PATTERN_HELP = (
    "Adapter IDs must start with a letter or digit and contain only "
    "letters, digits, dots (.), hyphens (-), and underscores (_)"
)


def adapter_id_problem(adapter_id: object) -> str | None:
    """Return why *adapter_id* is not a valid configured adapter identifier.

    Parameters
    ----------
    adapter_id:
        Candidate identifier from any configuration seam (YAML mapping
        key, ``adapter_id`` field, or environment ``ADAPTER_ID`` value).

    Returns
    -------
    str | None
        A human-readable, actionable reason when the identifier is
        rejected, or ``None`` when it is valid.  Reasons never include
        secret material — identifiers are not secrets.
    """
    if not isinstance(adapter_id, str):
        return (
            f"adapter_id must be a string, got {type(adapter_id).__name__} "
            f"(quote numeric YAML keys and adapter_id values)"
        )
    if not adapter_id:
        return "adapter_id must not be empty"
    if len(adapter_id) > ADAPTER_ID_MAX_LENGTH:
        return (
            f"adapter_id must be at most {ADAPTER_ID_MAX_LENGTH} characters, "
            f"got {len(adapter_id)}"
        )
    if adapter_id.endswith("."):
        return f"adapter_id must not end with a period: got {adapter_id!r}"
    if _WINDOWS_RESERVED_NAME_RE.match(adapter_id) is not None:
        return (
            "adapter_id must not use a Windows-reserved device name "
            f"(CON, PRN, AUX, NUL, COM1-COM9, LPT1-LPT9): got {adapter_id!r}"
        )
    if _ADAPTER_ID_RE.fullmatch(adapter_id) is None:
        return f"{_PATTERN_HELP}: got {adapter_id!r}"
    return None
