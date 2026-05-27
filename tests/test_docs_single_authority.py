"""Single-authority documentation guard tests.

Asserts that the documentation does not contain scattered authority
claims outside the spec/ tree:

  1. No "single source of truth" claims outside docs/spec/.
  2. No "this contract defines" authority language outside docs/spec/.

Legacy directories (contracts/, runbooks/, architecture/, releases/)
are excluded from scans as they will be deleted.

The spec/ tree is the sole authority for behavioral claims.  Operator
docs (ops/) and developer docs (dev/) describe usage and extension —
they do not define semantics or declare themselves authoritative.
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

#: Directories where "single source of truth" language is permitted.
_SPEC_DIR = _DOCS_DIR / "spec"

#: Old directories that will be deleted — exclude from scans.
_LEGACY_DIRS = [
    _DOCS_DIR / "contracts",
    _DOCS_DIR / "runbooks",
    _DOCS_DIR / "architecture",
    _DOCS_DIR / "releases",
    _DOCS_DIR / "STATUS.md",
    _DOCS_DIR / "ARCHITECTURE_PLAN.md",
]


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _is_legacy(path: Path) -> bool:
    """Check if a path is under a legacy directory (contracts/, runbooks/)."""
    for legacy_dir in _LEGACY_DIRS:
        try:
            path.relative_to(legacy_dir)
            return True
        except ValueError:
            pass
    return False


def _all_md_files() -> list[Path]:
    """Collect all .md files under docs/ recursively, sorted.
    Excludes legacy directories (contracts/, runbooks/, architecture/, releases/)."""
    if not _DOCS_DIR.is_dir():
        return []
    return sorted(
        f for f in _DOCS_DIR.rglob("*.md")
        if not _is_legacy(f)
    )


def _is_under_spec(path: Path) -> bool:
    """Check if a path is under docs/spec/."""
    try:
        path.relative_to(_DOCS_DIR / "spec")
        return True
    except ValueError:
        return False


# ===========================================================================
# 1. No "single source of truth" outside docs/spec/README.md
# ===========================================================================


class TestNoSingleSourceOfTruth:
    """The phrase "single source of truth" is permitted throughout docs/spec/
    where it makes normative claims about authority.  Operator and developer
    docs must not make this claim."""

    _SSOT_PATTERNS: list[re.Pattern[str]] = [
        re.compile(r"single\s+source\s+of\s+truth", re.IGNORECASE),
        re.compile(r"SSOT", re.IGNORECASE),
    ]

    @pytest.mark.parametrize(
        "md_file",
        [f for f in _all_md_files() if not _is_under_spec(f)],
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_single_source_of_truth_outside_spec(
        self, md_file: Path
    ) -> None:
        """No .md file outside docs/spec/ may contain
        'single source of truth' or 'SSOT'."""
        text = _read(md_file)
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in self._SSOT_PATTERNS:
                if pattern.search(line):
                    pytest.fail(
                        f"{md_file.relative_to(_ROOT)}:{lineno}: "
                        f"found '{pattern.pattern}' outside docs/spec/. "
                        f"The spec/ tree is the sole authority; do not "
                        f"claim 'single source of truth' elsewhere.\n"
                        f"  {line.strip()}"
                    )


# ===========================================================================
# 2. No "this contract defines" authority language outside spec/
# ===========================================================================


class TestNoContractAuthorityOutsideSpec:
    """Authority phrases like "this contract defines" must not appear
    outside docs/spec/.  Only spec/ documents define behavioral
    contracts."""

    _AUTHORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "this contract defines",
            re.compile(r"this\s+contract\s+defines", re.IGNORECASE),
        ),
        (
            "this document is the authoritative",
            re.compile(r"this\s+document\s+is\s+the\s+authoritative", re.IGNORECASE),
        ),
        (
            "this is the definitive",
            re.compile(r"this\s+is\s+the\s+definitive", re.IGNORECASE),
        ),
    ]

    @pytest.mark.parametrize(
        "md_file",
        [f for f in _all_md_files() if not _is_under_spec(f)],
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_authority_language_outside_spec(self, md_file: Path) -> None:
        """No .md file outside docs/spec/ may contain contract-authority
        language."""
        text = _read(md_file)
        for label, pattern in self._AUTHORITY_PATTERNS:
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    pytest.fail(
                        f"{md_file.relative_to(_ROOT)}:{lineno}: "
                        f"found authority phrase '{label}' outside docs/spec/. "
                        f"Only spec/ documents define behavioral contracts.\n"
                        f"  {line.strip()}"
                    )
