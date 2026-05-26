"""Contract taxonomy guard tests.

Asserts structural invariants of the docs/contracts/ directory:
  1. Every contract .md file (except README.md) is listed in README.md.
  2. Every contract .md file has a disposition/status header in its first
     20 lines (``> **Status:**``, ``> Status:``, or ``**Status:**``).
  3. Stale taxonomy guard: removed/resolved terms must not appear as
     current enum members or defined values in source code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = _ROOT / "docs" / "contracts"
README = CONTRACTS_DIR / "README.md"

_STATUS_RE = re.compile(
    r"^\s*(>\s*)?(\*\*)?Status:(\*\*)?\s",
    re.MULTILINE,
)

_OWNED_BY_OTHERS_RE = re.compile(
    r"^## Files Owned by Other Agents\s*\n" r"(.*?)(?=\n## |\Z)",
    re.DOTALL | re.MULTILINE,
)


def _contract_files() -> list[Path]:
    """All .md files in docs/contracts/ except README.md, sorted."""
    return sorted(p for p in CONTRACTS_DIR.glob("*.md") if p.name != "README.md")


def _files_owned_by_others() -> set[str]:
    """Parse the 'Files Owned by Other Agents' section from README.md."""
    readme_text = _read(README)
    m = _OWNED_BY_OTHERS_RE.search(readme_text)
    if not m:
        return set()
    section = m.group(1)
    return set(re.findall(r"`([^`\n]+\.md)`", section))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ===========================================================================
# 1. Every contract file is listed in README.md
# ===========================================================================


class TestContractReadmeIndex:
    """Each contract file must appear in the README taxonomy index."""

    @pytest.fixture(autouse=True)
    def _readme_text(self) -> None:
        self._readme = _read(README)

    @pytest.mark.parametrize(
        "contract",
        _contract_files(),
        ids=lambda p: p.name,
    )
    def test_contract_listed_in_readme(self, contract: Path) -> None:
        """Contract filename must appear somewhere in README.md."""
        assert contract.name in self._readme, (
            f"{contract.name} not found in {README.name}. "
            f"Every contract file must be listed in the taxonomy index."
        )


# ===========================================================================
# 2. Every contract has a status/disposition header
# ===========================================================================


class TestContractStatusHeader:
    """Each contract must carry a status or disposition header."""

    #: Number of lines from file start to search for a status header.
    _HEADER_WINDOW = 20

    @pytest.fixture(autouse=True)
    def _load_exclusions(self) -> None:
        self._owned_by_others = _files_owned_by_others()

    @pytest.mark.parametrize(
        "contract",
        _contract_files(),
        ids=lambda p: p.name,
    )
    def test_has_status_header(self, contract: Path) -> None:
        """First N lines must contain a Status: line (block-quoted or bare).

        Files listed under 'Files Owned by Other Agents' in README.md are
        skipped — they have not yet received disposition headers.
        """
        if contract.name in self._owned_by_others:
            pytest.skip(
                f"{contract.name} is owned by another agent (no disposition header yet)"
            )
        head = "\n".join(_read(contract).splitlines()[: self._HEADER_WINDOW])
        assert _STATUS_RE.search(head), (
            f"{contract.name} has no status/disposition header in its first "
            f"{self._HEADER_WINDOW} lines. Expected one of: "
            f"`> **Status:** ...`, `> Status: ...`, or `**Status:** ...`."
        )


# ===========================================================================
# 3. Stale taxonomy guard
# ===========================================================================

#: Terms that were removed/renamed and must not re-appear as current
#: enum members or defined values in source.  This is intentionally a
#: small, focused set — add entries only when a term is explicitly
#: removed by an Oracle review or cleanup action.
_STALE_TERMS: list[str] = ["TARGET_NOT_FOUND", "DUPLICATE_SUPPRESSED"]

#: Regex that matches enum-member-like definitions in Python source.
_ENUM_MEMBER_RE = re.compile(r"^\s+(\w+)\s*[:=]")

#: Directories to scan for stale-term reappearance.
_SRC_DIRS = [_ROOT / "src", _ROOT / "tests"]


class TestStaleTaxonomyGuard:
    """Removed/resolved terms must not re-appear as defined enum members."""

    @pytest.mark.skipif(
        not _STALE_TERMS,
        reason="No stale terms registered — guard is a no-op until terms are added.",
    )
    @pytest.mark.parametrize("term", _STALE_TERMS)
    def test_stale_term_not_in_source_enums(self, term: str) -> None:
        """A removed term must not appear as a current enum member."""
        for src_dir in _SRC_DIRS:
            if not src_dir.exists():
                continue
            for py_file in src_dir.rglob("*.py"):
                text = _read(py_file)
                for lineno, line in enumerate(text.splitlines(), start=1):
                    # Only flag if the line looks like an enum/class member assignment.
                    m = _ENUM_MEMBER_RE.match(line)
                    if m and m.group(1).lower() == term.lower():
                        pytest.fail(
                            f"{py_file.relative_to(_ROOT)}:{lineno}: "
                            f"stale term '{term}' appears as a defined member. "
                            f"This term was removed and must not be reintroduced."
                        )
