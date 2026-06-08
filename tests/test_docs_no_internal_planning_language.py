"""Internal planning-language guard tests.

Asserts that normative and operational documentation does not contain
internal planning-cycle vocabulary.  Terms like "tranche", "boulder",
"sprint", and other internal process labels are planning artifacts and
MUST NOT appear in spec/, ops/, or dev/ documentation.  They are
permitted only in docs/changes/ which tracks historical change fragments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.forbidden_terms import FORBIDDEN_TERMS, find_stale_terms

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"

#: Directories that MUST NOT contain internal planning vocabulary.
_SCANNED_DIRS: list[Path] = [
    _DOCS_DIR / "spec",
    _DOCS_DIR / "ops",
    _DOCS_DIR / "dev",
]

#: Additional planning-cycle terms specific to this test (not shared with
#: release-readiness convergence tests).  Combined with FORBIDDEN_TERMS
#: from the shared helper.
_PLANNING_CYCLE_TERMS: list[re.Pattern[str]] = [
    re.compile(r"\btranche\b", re.IGNORECASE),
    re.compile(r"\bboulder\b", re.IGNORECASE),
    re.compile(r"\bsprint\b", re.IGNORECASE),
]

#: Full set of patterns scanned by this test suite.
_ALL_PATTERNS: list[re.Pattern[str]] = _PLANNING_CYCLE_TERMS + FORBIDDEN_TERMS


# ===========================================================================
# No internal planning vocabulary in scanned directories
# ===========================================================================


#: Files where mentions of planning terms are allowed (style guides explaining
#: what NOT to use).
_EXEMPT_FILES: set[str] = {
    "documentation-style.md",
    "change-process.md",
}


def _is_exempt(md_file: Path) -> bool:
    """Check if a file is exempt from planning-language checks."""
    return md_file.name in _EXEMPT_FILES


class TestNoInternalPlanningLanguage:
    """Internal planning-cycle terms must not appear in spec/, ops/, or dev/."""

    @pytest.mark.parametrize(
        "scan_dir",
        _SCANNED_DIRS,
        ids=lambda d: d.name,
    )
    def test_no_tranche_in_directory(self, scan_dir: Path) -> None:
        """Scan all .md files in the directory for forbidden planning terms.

        Reports the file path and line number of each violation.
        Style guide files that mention terms in a "do not use" context are
        exempt.
        """
        if not scan_dir.is_dir():
            pytest.skip(f"Directory not found: {scan_dir.relative_to(_ROOT)}")

        raw = find_stale_terms([scan_dir.name], _ALL_PATTERNS)
        violations = [
            f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
            for md_file, lineno, content in raw
            if not _is_exempt(md_file)
        ]

        if violations:
            pytest.fail(
                f"Found internal planning-cycle vocabulary in "
                f"{scan_dir.relative_to(_ROOT)}/. "
                f"These terms are permitted only in docs/changes/:\n"
                + "\n".join(violations)
            )

    def test_tranche_allowed_in_changes_directory(self) -> None:
        """docs/changes/ is an allowlisted directory — no assertion needed.

        This test serves as documentation: 'tranche' is permitted in
        docs/changes/ because change fragments may reference historical
        planning language.
        """
        changes_dir = _DOCS_DIR / "changes"
        if not changes_dir.is_dir():
            pytest.skip("docs/changes/ not found")
        # No assertion — changes/ is exempt from planning-language checks.
