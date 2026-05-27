"""Documentation link integrity tests.

Asserts that:

  1. All relative markdown links in docs/ point to existing files.
  2. No links point to legacy paths (docs/contracts/ or docs/runbooks/).
  3. No bare prose references to legacy paths appear outside the legacy
     directories themselves.
  4. Broken links are reported with file path and line number.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"

# Regex captures the destination of a markdown link: [text](dest)
_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)]+)\)")

# Legacy directories that must not be referenced.
_FORBIDDEN_PREFIXES = ("docs/contracts/", "docs/runbooks/")

# Regex matching bare references to legacy paths in prose.
_LEGACY_PROSE_RE = re.compile(
    r"(docs/contracts/|docs/runbooks/)\S*",
)

# Legacy directories to exclude from scans (will be deleted).
_LEGACY_DIRS = [
    _DOCS_DIR / "contracts",
    _DOCS_DIR / "runbooks",
    _DOCS_DIR / "architecture",
    _DOCS_DIR / "releases",
    _DOCS_DIR / "STATUS.md",
    _DOCS_DIR / "ARCHITECTURE_PLAN.md",
    _DOCS_DIR / "spec" / "modular-event-engine-spec.md",  # old master spec, to be archived
]


def _is_legacy(path: Path) -> bool:
    """Check if a path is under a legacy directory."""
    for legacy_dir in _LEGACY_DIRS:
        if legacy_dir.is_file() and path == legacy_dir:
            return True
        try:
            path.relative_to(legacy_dir)
            return True
        except ValueError:
            pass
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_md_files() -> list[Path]:
    """Collect every .md file under docs/, excluding legacy directories."""
    if not _DOCS_DIR.is_dir():
        return []
    return sorted(
        f for f in _DOCS_DIR.rglob("*.md")
        if not _is_legacy(f)
    )


def _relative(path: Path) -> str:
    return str(path.relative_to(_ROOT))


def _extract_links(
    filepath: Path,
) -> list[tuple[int, str]]:
    """Return (line_number, target) for each markdown link in *filepath*."""
    results: list[tuple[int, str]] = []
    text = filepath.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in _LINK_RE.finditer(line):
            target = match.group(1).strip()
            # Skip empty targets and fragment-only links.
            if not target or target.startswith("#"):
                continue
            results.append((line_number, target))
    return results


# ===========================================================================
# 1. All relative links point to existing files
# ===========================================================================


class TestDocLinks:
    """Markdown links in docs/ must resolve and must not target legacy paths."""

    @pytest.mark.parametrize(
        "filepath",
        _all_md_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_links_resolve(self, filepath: Path) -> None:
        """Every relative markdown link must point to an existing file."""
        source_dir = filepath.parent
        links = _extract_links(filepath)
        failures: list[str] = []

        for line_number, target in links:
            # Skip URLs (http, https, mailto, etc.)
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", target):
                continue

            # Separate fragment from path.
            path_part = target.split("#")[0]
            if not path_part:
                continue

            resolved = (source_dir / path_part).resolve()
            if not resolved.exists():
                failures.append(
                    f"{_relative(filepath)}:{line_number}: "
                    f"broken link '{target}' -> {_relative(resolved) if resolved.is_relative_to(_ROOT) else resolved}"
                )

        if failures:
            pytest.fail(
                "Broken markdown links found:\n  " + "\n  ".join(failures)
            )

    @pytest.mark.parametrize(
        "filepath",
        _all_md_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_legacy_path_references(self, filepath: Path) -> None:
        """Links must not reference docs/contracts/ or docs/runbooks/."""
        links = _extract_links(filepath)
        failures: list[str] = []

        for line_number, target in links:
            normalised = target.replace("\\", "/")
            for prefix in _FORBIDDEN_PREFIXES:
                if prefix in normalised:
                    failures.append(
                        f"{_relative(filepath)}:{line_number}: "
                        f"link targets legacy path '{target}' ({prefix}*)"
                    )

        if failures:
            pytest.fail(
                "Legacy path references found:\n  " + "\n  ".join(failures)
            )


# ===========================================================================
# 2. No bare prose references to legacy paths
# ===========================================================================


class TestNoLegacyPathProseReferences:
    """Markdown files must not contain bare path references to legacy
    directories docs/contracts/ or docs/runbooks/ in prose or code
    blocks. Files inside those legacy directories are exempt."""

    @pytest.mark.parametrize(
        "filepath",
        _all_md_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_legacy_path_prose_references(self, filepath: Path) -> None:
        """No bare references to docs/contracts/ or docs/runbooks/
        should appear in any docs/ markdown file outside those directories.
        Style guides and READMEs that reference the old system as
        'replaced' are exempt."""
        if _is_legacy(filepath):
            return

        # Exempt files that reference legacy paths in a "replaced" context.
        exempt_names = {"documentation-style.md", "README.md", "change-process.md"}
        if filepath.name in exempt_names:
            return

        text = filepath.read_text(encoding="utf-8")
        failures: list[str] = []

        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _LEGACY_PROSE_RE.finditer(line):
                failures.append(
                    f"{_relative(filepath)}:{lineno}: "
                    f"reference to legacy path '{match.group()}'"
                )

        if failures:
            pytest.fail(
                f"Found {len(failures)} reference(s) to legacy paths "
                f"(use docs/spec/, docs/ops/, or docs/dev/ instead):\n  "
                + "\n  ".join(failures)
            )
