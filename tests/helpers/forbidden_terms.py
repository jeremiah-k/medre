"""Shared forbidden-term scanning helpers for documentation tests."""

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


def find_stale_terms(
    scan_paths: list[str],
    patterns: list[re.Pattern[str]],
) -> list[tuple[Path, int, str]]:
    """Scan .md files under the given doc subdirectories for forbidden terms.

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
            text = md_file.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern in patterns:
                    if pattern.search(line):
                        violations.append((md_file, lineno, line.strip()))
    return violations
