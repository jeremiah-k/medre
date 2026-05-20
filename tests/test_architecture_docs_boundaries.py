"""Boundary tests for documentation — no active docs may reference stale paths.

Checks that documentation files use current import paths for modules
that were moved with no backward-compatibility re-export:

- ``medre.runtime.capacity`` / ``src/medre/runtime/capacity.py`` → ``medre.core.runtime.capacity`` / ``src/medre/core/runtime/capacity.py``

Note: ``medre.observability.sanitization`` is intentionally kept as a
user-facing re-export and is NOT flagged as stale.
"""

from __future__ import annotations

from pathlib import Path

# Docs directories to scan.
_DOC_DIRS = [
    "docs",
]

# Stale path patterns that should NOT appear in active docs (without historical qualifier).
# medre.observability.sanitization is intentionally NOT listed — the re-export
# at that path was preserved as the user-facing API.
_STALE_PATTERNS: list[tuple[str, str]] = [
    ("medre.runtime.capacity", "Use medre.core.runtime.capacity"),
    ("src/medre/runtime/capacity.py", "Use src/medre/core/runtime/capacity.py"),
]

# Words that indicate historical context — stale references on a line with
# any of these words are exempt.
_HISTORICAL_WORDS = frozenset(
    {
        "previously",
        "formerly",
        "moved from",
        "moved to",
        "historical",
        "deprecated",
        "was previously",
        "was formerly",
        "was moved",
    }
)


def _is_historical_line(line: str) -> bool:
    """Return True if *line* contains an explicit historical qualifier."""
    lowered = line.lower()
    return any(word in lowered for word in _HISTORICAL_WORDS)


def _find_stale_references(
    repo_root: Path,
) -> list[tuple[str, int, str, str]]:
    """Return ``(rel_path, line_no, content, hint)`` for stale refs."""
    violations: list[tuple[str, int, str, str]] = []
    for doc_dir in _DOC_DIRS:
        base = repo_root / doc_dir
        if not base.is_dir():
            continue
        for md_file in sorted(base.rglob("*.md")):
            rel = str(md_file.relative_to(repo_root))
            for i, line in enumerate(md_file.read_text().splitlines(), start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                # Allow historical qualifiers.
                if _is_historical_line(stripped):
                    continue
                for pattern, hint in _STALE_PATTERNS:
                    if pattern in stripped:
                        violations.append((rel, i, stripped, hint))
    return violations


class TestActiveDocsNoStalePaths:
    """Active documentation must not reference moved module paths."""

    def test_no_stale_capacity_paths(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        violations = _find_stale_references(repo_root)
        assert not violations, "Stale module paths found in active docs:\n" + "\n".join(
            f"  {f}:{ln}: {line}\n    → {hint}" for f, ln, line, hint in violations
        )
