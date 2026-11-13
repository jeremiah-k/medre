"""Allowlist for the self-compatibility vocabulary guard over ``src/``.

The guard (``SELF_COMPAT_TERMS`` in ``tests/helpers/forbidden_terms.py``)
bans phrase framing that presents MEDRE's own surfaces as retained
compatibility baggage. A foreign SDK, protocol, or vendor contract may
genuinely require wording that trips those phrases — for example when a
vendor's error text or a wire-compatible field name must be quoted verbatim.
This module is the ONLY place such exceptions may live.

Entry shape — tight pairs:

    Each entry is a ``(path prefix, phrase pattern)`` pair plus the name of
    the foreign contract it excuses. Both halves must match for a line to be
    excused: the file's ``src/``-relative posix path must fall under the
    prefix, and the exact line must match the entry's pattern. Whole-file
    exemptions are never granted.

Self-asserting:

    An entry whose allowlisted text no longer exists under its prefix is
    stale. ``stale_entries`` detects this, and the guard test fails on any
    stale entry, so exceptions die loudly when the foreign wording they
    excuse is cleaned up or moved. Entries are never silently retired.

Expected state:

    The allowlist ships EMPTY: ``src/`` scans clean at these phrases. The
    machinery is exercised with synthetic entries against a synthetic tree
    by ``tests/test_src_self_compat_vocabulary.py`` so both halves of the
    contract (carve-out and staleness) stay proven even while the list is
    empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tests.helpers.forbidden_terms import find_forbidden_in_tree


@dataclass(frozen=True)
class SelfCompatKeep:
    """One allowlisted phrase scoped to one ``src/`` path prefix.

    Attributes:
        path_prefix: Posix path prefix relative to ``src/`` (``medre/...``).
            Matches the exact file, or every file under a directory prefix.
        pattern: Compiled phrase that must match the exact line being
            excused. Typically a copy of the banned pattern, optionally
            narrowed to the foreign text actually present.
        contract: Name of the foreign contract whose own wording the entry
            excuses. Surfaced in guard failures so exceptions stay auditable.
    """

    path_prefix: str
    pattern: re.Pattern[str]
    contract: str


#: Foreign-contract exceptions. Intentionally empty: ``src/`` scans clean
#: at the banned phrases. Add an entry only for wording a foreign contract
#: genuinely requires, never for MEDRE's own prose.
SELF_COMPAT_KEEP_PATHS: tuple[SelfCompatKeep, ...] = ()


def _under_prefix(rel_posix: str, path_prefix: str) -> bool:
    """Return True if ``rel_posix`` is the prefix path or lies under it."""
    normalized = path_prefix.rstrip("/")
    return rel_posix == normalized or rel_posix.startswith(normalized + "/")


def excusing_contract(
    rel_posix: str,
    line: str,
    entries: tuple[SelfCompatKeep, ...] = SELF_COMPAT_KEEP_PATHS,
) -> str | None:
    """Return the foreign contract excusing this path/line pair, or None.

    A pair excuses the line only when BOTH halves match: the ``src/``-relative
    posix path falls under the entry's prefix AND the line matches the
    entry's phrase pattern.
    """
    for entry in entries:
        if _under_prefix(rel_posix, entry.path_prefix) and entry.pattern.search(line):
            return entry.contract
    return None


def stale_entries(
    roots: list[Path],
    entries: tuple[SelfCompatKeep, ...] = SELF_COMPAT_KEEP_PATHS,
) -> list[SelfCompatKeep]:
    """Return entries whose allowlisted text no longer exists under ``roots``.

    An entry is stale when no ``.py`` line under its path prefix matches its
    phrase pattern. Scanning reuses the shared tree walker so never-scan
    directories and binary/unreadable files are handled identically to the
    guard itself.
    """
    stale: list[SelfCompatKeep] = []
    for entry in entries:
        hits = find_forbidden_in_tree(roots, [entry.pattern], extensions=(".py",))
        found = False
        for path, _lineno, _line in hits:
            for root in roots:
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                if _under_prefix(rel, entry.path_prefix):
                    found = True
                    break
            if found:
                break
        if not found:
            stale.append(entry)
    return stale
