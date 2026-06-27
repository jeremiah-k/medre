"""Shared forbidden-term scanning helpers for documentation and code tests.

This module is the single source of truth for the internal vocabulary that
MUST NOT appear in durable artifacts. The blocked patterns are stored as
concatenated string fragments so that no complete blocked word appears as a
literal substring of this file — the file enforces the ban without itself
containing the banned text.

Policy scope (see ``docs/dev/documentation-style.md``):
    Forbidden in durable docs, code comments/docstrings, test names, test
    methods, test comments, test docstrings, test filenames, example configs,
    branch names, new commit messages, and agent-facing prompts.

Scan scope (default):
    - ``docs/``  — all ``.md`` files (spec, ops, dev, schemas, changes)
    - ``src/``   — all ``.py`` files (comments, docstrings)
    - ``tests/`` — all ``.py`` files (names, methods, comments, docstrings)
    - ``examples/`` — ``.md``, ``.py``, ``.toml``, ``.yaml``, ``.yml``, ``.json``
    - Filenames themselves are scanned across the above trees.

Not scanned:
    - ``.git/`` (history is preserved; the policy does not rewrite history)
    - ``__pycache__/``, build artifacts, virtualenvs

No definitional carve-out:
    Every pattern below is assembled from fragments at import time, so this
    module scans clean against its own compiled patterns. The style guide
    and enforcer test likewise contain no literal blocked word, so
    ``DEFINITIONAL_EXEMPT_FILES`` is intentionally empty.
"""

from __future__ import annotations

import re
from pathlib import Path

# Root of the project (this file is at tests/helpers/forbidden_terms.py).
_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _ROOT / "docs"

#: Forbidden internal terms shared across test suites.
#: Both test_docs_no_internal_planning_language and
#: test_release_readiness_convergence scan for these patterns. Each pattern
#: is built from concatenated fragments so no blocked phrase appears as a
#: literal substring of this source file.
FORBIDDEN_TERMS: list[re.Pattern[str]] = [
    re.compile(r"\balpha " + r"stage\b", re.IGNORECASE),
    re.compile(r"\bbeta " + r"period\b", re.IGNORECASE),
    re.compile(r"\bbeta " + r"contractual\b", re.IGNORECASE),
    re.compile(r"\bPC-" + r"targeted\b"),
    re.compile(r"\bLSP " + r"diagnostics\b", re.IGNORECASE),
    re.compile(r"\bworking " + r"tree " + r"clean\b", re.IGNORECASE),
    re.compile(r"\bfinal " + r"report\b", re.IGNORECASE),
    re.compile(r"\bagent-" + r"as-process\b", re.IGNORECASE),
    re.compile(r"\bfuture " + "tr" + "anche" + r"\b", re.IGNORECASE),
    # Stale alpha/beta branding in durable docs
    re.compile(r"\buntil " + r"beta\b", re.IGNORECASE),
    re.compile(r"\bStatus:" + r"\s*Alpha\b"),
    re.compile(r"\bE2EE Text " + r"Alpha\b", re.IGNORECASE),
    re.compile(r"\bMatrix Operation " + r"Alpha\b"),
    re.compile(r"\balpha[-_]" + r"walkthrough\b"),
    re.compile(r"\balpha-" + r"installation\b"),
    re.compile(r"\bAlpha " + r"validates\b"),
]

#: Internal vocabulary banned from durable artifacts. Kept separate from
#: FORBIDDEN_TERMS because release-readiness convergence tests scan only the
#: shared FORBIDDEN_TERMS list against docs, while the durable-language
#: enforcer applies the union of both lists everywhere. Each pattern is
#: built from concatenated fragments so no blocked word appears as a literal
#: substring of this source file.
PLANNING_CYCLE_TERMS: list[re.Pattern[str]] = [
    # Bare-substring patterns (no \b): match the two internal labels that
    # have no legitimate use in this mesh-relay codebase and would only
    # surface as planning artifacts. Bare matching catches digit- and
    # underscore-suffixed forms (e.g. a label followed by a digit) that a
    # word-bounded anchor would miss. Both labels are assembled from
    # fragments below.
    re.compile("po" + "nytail", re.IGNORECASE),
    re.compile("tr" + "anche", re.IGNORECASE),
    # Word-bounded patterns: the next two have ordinary English meanings
    # (a rock, a run) that appear in unrelated contexts, so they stay
    # anchored on both sides with \b. Fragmented so the literal word does
    # not appear in this file.
    re.compile(r"\b" + "bou" + "lder" + r"\b", re.IGNORECASE),
    re.compile(r"\b" + "sp" + "rint" + r"\b", re.IGNORECASE),
    # Numeric batch qualifiers (case-insensitive): an internal batch label
    # immediately followed by optional whitespace and a digit. Same class
    # of internal qualifier as the bare labels above.
    re.compile(r"\b(?:track" + r"|wave)\s*\d", re.IGNORECASE),
    # Letter-suffixed batch qualifiers (case-sensitive): a capitalized
    # batch label followed by a single uppercase letter. Deliberately
    # not IGNORECASE so ordinary lowercase usage ("part of", "part 1",
    # "parts are", "participate") does not trip the guard. Fragmented
    # so the literal word does not appear in this file.
    re.compile(r"\b" + "P" + r"art\s+[A-Z]\b"),
]

#: Union of all forbidden patterns — use for full-scope durable-artifact scans.
ALL_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = PLANNING_CYCLE_TERMS + FORBIDDEN_TERMS

#: Files that definitionally contain the forbidden words and are therefore
#: exempt from content scans. Intentionally empty: every blocked pattern is
#: assembled from string fragments in this module, the style guide
#: (``docs/dev/documentation-style.md``) describes the ban without enumerating
#: the blocked words, and the enforcer test
#: (``tests/test_docs_no_internal_planning_language.py``) describes its
#: behavior without naming them. All three definitional files now scan clean
#: against the compiled patterns, so no self-exemption is required.
DEFINITIONAL_EXEMPT_FILES: set[str] = set()

#: Directory names never scanned (history, caches, builds, venvs).
_NEVER_SCAN_DIRS: set[str] = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
    "build",
    "dist",
    ".eggs",
    "medre.egg-info",
}

#: File extensions scanned when walking source/test/example trees.
_SCANNED_EXTENSIONS: tuple[str, ...] = (
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".txt",
    ".cfg",
    ".ini",
)


def _is_exempt(path: Path, exempt_basenames: set[str]) -> bool:
    """Return True if ``path``'s basename is in the definitional exempt set."""
    return path.name in exempt_basenames


def find_stale_terms(
    scan_paths: list[str],
    patterns: list[re.Pattern[str]],
) -> list[tuple[Path, int, str]]:
    """Scan .md files under the given doc subdirectories for forbidden terms.

    Backward-compatible narrow scanner: walks ``docs/<subdir>`` for ``*.md``
    files only. Used by release-readiness convergence tests that scope their
    check to spec/ and ops/ docs.

    Args:
        scan_paths: Subdirectory names under ``docs/`` to scan
            (e.g. ``["spec/", "ops/"]``).
        patterns: Compiled regex patterns to search for.

    Returns:
        List of ``(file_path, line_number, line_content)`` tuples
        for each match found.
    """
    violations: list[tuple[Path, int, str]] = []
    for subdir in scan_paths:
        directory = _DOCS_DIR / subdir
        if not directory.is_dir():
            continue
        for md_file in sorted(directory.rglob("*.md")):
            if _is_exempt(md_file, DEFINITIONAL_EXEMPT_FILES):
                continue
            text = md_file.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern in patterns:
                    if pattern.search(line):
                        violations.append((md_file, lineno, line.strip()))
    return violations


def find_forbidden_in_tree(
    roots: list[Path],
    patterns: list[re.Pattern[str]],
    *,
    extensions: tuple[str, ...] = _SCANNED_EXTENSIONS,
    exempt_basenames: set[str] = DEFINITIONAL_EXEMPT_FILES,
) -> list[tuple[Path, int, str]]:
    """Scan file CONTENTS under arbitrary directory trees for forbidden terms.

    Walks each root recursively, skipping ``_NEVER_SCAN_DIRS`` and any file
    whose basename is in ``exempt_basenames``. Reads each file whose extension
    is in ``extensions`` and reports lines matching any pattern.

    Args:
        roots: Directory paths to walk (e.g. ``[ROOT / "src", ROOT / "tests"]``).
        patterns: Compiled regex patterns to search for.
        extensions: File extensions to scan (default: durable text types).
        exempt_basenames: Basenames to skip (definitional files).

    Returns:
        Sorted list of ``(file_path, line_number, line_content)`` tuples.
    """
    violations: list[tuple[Path, int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in extensions:
                continue
            if _is_exempt(path, exempt_basenames):
                continue
            # Skip any file nested under a never-scan directory.
            if any(part in _NEVER_SCAN_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # Binary or unreadable file — not a durable text artifact.
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern in patterns:
                    if pattern.search(line):
                        violations.append((path, lineno, line.strip()))
    return violations


def find_forbidden_in_filenames(
    roots: list[Path],
    patterns: list[re.Pattern[str]],
    *,
    exempt_basenames: set[str] = DEFINITIONAL_EXEMPT_FILES,
) -> list[tuple[Path, str]]:
    """Scan file PATHS (names) under directory trees for forbidden terms.

    Tests each file's full relative path string (not contents) against the
    patterns. Catches forbidden words embedded in filenames or directory
    names — for example, a test file previously carried a blocked batch
    qualifier in its name and was renamed to ``test_lxmf_session_callback_guards.py``;
    this scanner protects against regressing that fix.

    Args:
        roots: Directory paths to walk.
        patterns: Compiled regex patterns to search for in the path string.
        exempt_basenames: Basenames to skip (definitional files).

    Returns:
        Sorted list of ``(file_path, matched_path_string)`` tuples.
    """
    violations: list[tuple[Path, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if _is_exempt(path, exempt_basenames):
                continue
            if any(part in _NEVER_SCAN_DIRS for part in path.parts):
                continue
            # Scan the full relative path so directory-name violations surface
            # too. Use forward slashes for stable matching across platforms.
            rel = path.relative_to(_ROOT).as_posix()
            for pattern in patterns:
                if pattern.search(rel):
                    violations.append((path, rel))
                    break
    return violations
