"""Shared forbidden-term scanning helpers for documentation and code tests.

This module is the single source of truth for the internal planning-cycle
vocabulary that MUST NOT appear in durable artifacts.

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

Definitional carve-out:
    The forbidden WORDS legitimately live in this file (``FORBIDDEN_TERMS``,
    ``PLANNING_CYCLE_TERMS``) and in the style guide that documents the ban
    (``docs/dev/documentation-style.md``). These files are exempt from
    content scans so they do not flag themselves. The enforcer test module
    is also exempt. See ``DEFINITIONAL_EXEMPT_FILES``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Root of the project (this file is at tests/helpers/forbidden_terms.py).
_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _ROOT / "docs"

#: Forbidden internal planning terms shared across test suites.
#: Both test_docs_no_internal_planning_language and
#: test_release_readiness_convergence scan for these patterns.
FORBIDDEN_TERMS: list[re.Pattern[str]] = [
    re.compile(r"\balpha stage\b", re.IGNORECASE),
    re.compile(r"\bbeta period\b", re.IGNORECASE),
    re.compile(r"\bbeta contractual\b", re.IGNORECASE),
    re.compile(r"\bPC-targeted\b"),
    re.compile(r"\bLSP diagnostics\b", re.IGNORECASE),
    re.compile(r"\bworking tree clean\b", re.IGNORECASE),
    re.compile(r"\bfinal report\b", re.IGNORECASE),
    re.compile(r"\bagent-as-process\b", re.IGNORECASE),
    re.compile(r"\bfuture tranche\b", re.IGNORECASE),
    # Stale alpha/beta branding in durable docs
    re.compile(r"\buntil beta\b", re.IGNORECASE),
    re.compile(r"\bStatus:\s*Alpha\b"),
    re.compile(r"\bE2EE Text Alpha\b", re.IGNORECASE),
    re.compile(r"\bMatrix Operation Alpha\b"),
    re.compile(r"\balpha-walkthrough\b"),
    re.compile(r"\balpha-installation\b"),
    re.compile(r"\bAlpha validates\b"),
]

#: Internal planning-cycle vocabulary banned from durable artifacts.
#: Kept separate from FORBIDDEN_TERMS because release-readiness convergence
#: tests scan only the shared FORBIDDEN_TERMS list against docs, while the
#: durable-language enforcer applies the union of both lists everywhere.
PLANNING_CYCLE_TERMS: list[re.Pattern[str]] = [
    # Bare substring (no \b) — catches ponytail, Ponytail, _ponytail_,
    # tranche, tranche1, tranche6, Tranches, _tranche_, Tranche6Foo, etc.
    # Neither word has a legitimate use in this mesh-relay codebase, so a
    # substring match has no false positives.
    # boulder/sprint stay word-bounded: they have real English meanings
    # (a rock, a run) that appear in unrelated contexts.
    re.compile(r"ponytail", re.IGNORECASE),
    re.compile(r"tranche", re.IGNORECASE),
    re.compile(r"\bboulder\b", re.IGNORECASE),
    re.compile(r"\bsprint\b", re.IGNORECASE),
]

#: Union of all forbidden patterns — use for full-scope durable-artifact scans.
ALL_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = PLANNING_CYCLE_TERMS + FORBIDDEN_TERMS

#: Files that definitionally contain the forbidden words and are therefore
#: exempt from content scans. These are the files that DEFINE the ban or
#: enumerate the banned terms in a "do not use" context.
DEFINITIONAL_EXEMPT_FILES: set[str] = {
    # This helper: defines the regex patterns containing the forbidden words.
    "forbidden_terms.py",
    # The enforcer test: names the forbidden words in its docstring and
    # test logic.
    "test_docs_no_internal_planning_language.py",
    # The style guide: enumerates the banned terms to document the ban.
    "documentation-style.md",
}

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
    names (e.g. ``test_lxmf_session_tranche6.py``).

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
