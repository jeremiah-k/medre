"""Documentation link integrity tests.

Asserts that:

  1. All relative markdown links in docs/ point to existing files.
  2. No links point to legacy paths (docs/contracts/ or docs/runbooks/).
  3. No bare prose references to legacy paths appear outside the legacy
     directories themselves.
  4. Broken links are reported with file path and line number.
  5. Root-level build/config files (pyproject.toml and siblings) do not
     reference the removed docs/contracts/ or docs/runbooks/ trees.
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
    _DOCS_DIR
    / "spec"
    / "modular-event-engine-spec.md",  # old master spec, to be archived
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
    return sorted(f for f in _DOCS_DIR.rglob("*.md") if not _is_legacy(f))


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
            pytest.fail("Broken markdown links found:\n  " + "\n  ".join(failures))

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
            pytest.fail("Legacy path references found:\n  " + "\n  ".join(failures))


# ===========================================================================
# 2. No bare prose references to legacy paths
# ===========================================================================


class TestNoLegacyPathProseReferences:
    """Markdown files must not contain bare path references to legacy
    directories docs/contracts/ or docs/runbooks/ in prose or code
    blocks. Files inside those legacy directories are exempt.

    Carve-out (narrowed): a line is exempt ONLY when it clearly
    describes the removal, migration, or replacement of the legacy
    tree. ``_is_removal_context`` decides this by requiring BOTH (a)
    a removal keyword from ``_REMOVAL_KEYWORDS`` AND (b) the absence
    of any live-reference indicator from
    ``_LIVE_REFERENCE_INDICATORS``. The double guard is what separates
    a genuine removal note from a live reference wearing a removal
    adjective.

    Exempt (removal context — keyword present, no live indicator)::

        stale docs/runbooks/ references repointed at docs/ops/
        removed a reference to docs/contracts/foo.md
        prevents docs/contracts/ and docs/runbooks/ references from ...
        docs/contracts/25-matrix-e2ee-readiness.md (no durable replacement)

    Flagged (live reference — either a live indicator is present next
    to a removal adjective, or no removal keyword exists at all)::

        legacy docs/runbooks/foo.md still has details
        see docs/runbooks/foo.md for details
        docs/contracts/bar.md is in the legacy tree
        refer to docs/runbooks/baz.md

    A small set of style/process files (``documentation-style.md``,
    ``README.md``, ``change-process.md``) are fully exempt via
    ``exempt_names`` because they routinely reference the old system
    by name.
    """

    @pytest.mark.parametrize(
        "filepath",
        _all_md_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_no_legacy_path_prose_references(self, filepath: Path) -> None:
        """No bare references to docs/contracts/ or docs/runbooks/
        should appear in any docs/ markdown file outside those directories.
        Style guides and READMEs that reference the old system as
        'replaced' are exempt, and any line that is genuine removal
        context per ``_is_removal_context`` (removal keyword AND no
        live-reference indicator) is exempt as textual migration
        context."""
        if _is_legacy(filepath):
            return

        # Exempt files that reference legacy paths in a "replaced" context.
        exempt_names = {"documentation-style.md", "README.md", "change-process.md"}
        if filepath.name in exempt_names:
            return

        text = filepath.read_text(encoding="utf-8")
        failures: list[str] = []

        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_removal_context(line):
                continue
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


# ===========================================================================
# 3. Root-level build/config files must not reference removed doc trees
# ===========================================================================


# Root-level project metadata files that may declare paths into docs/. The
# docs/ tree itself is covered by the classes above; this list covers
# source-of-truth files at the repository root. Files that do not exist
# are skipped at runtime, so adding one later automatically opts it in.
_ROOT_CONFIG_FILES = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "Makefile",
]

# Removal keywords — a NECESSARY but no longer SUFFICIENT signal that a line
# is textual context about the migration rather than a live link. A line is
# only treated as removal context when it carries one of these AND does NOT
# carry a live-reference indicator (see ``_is_removal_context``). The double
# guard is what separates "stale docs/runbooks/ references repointed at
# docs/ops/" (exempt) from "legacy docs/runbooks/foo.md still has details"
# (flagged): both carry a removal adjective next to the path, but only the
# latter points the reader at the path as if it still exists.
# Shared by both the root-config scan (TestNoLegacyPathReferencesInRootConfig)
# and the prose-reference scan (TestNoLegacyPathProseReferences) so a single
# notion of "removal context" governs both checks.
_REMOVAL_KEYWORDS = (
    "replaced",
    "replacement",
    "removed",
    "legacy",
    "former",
    "migrated",
    "repointed",
    "stale",
    "instead",
    "prevent",
    "do not reference",
)

# Live-reference indicators — phrases that direct the reader to a path as if
# it still exists. When a line carries a legacy path AND a removal keyword
# BUT ALSO one of these indicators, it is treated as a live reference (not
# removal context) and flagged. Matched as substrings on the lower-cased
# line; the guard only runs on lines that already carry a legacy path, so the
# blast radius of common words like "details" is limited to legacy-path
# lines that also carry a removal keyword.
_LIVE_REFERENCE_INDICATORS = (
    "refer to",
    "for details",
    "for more",
    "is in",
    "lives in",
    "located at",
    "found in",
    "documented in",
    "described in",
    "still has",
    "still exists",
    "still in",
    "can be found",
    "has details",
)


def _is_removal_context(line: str) -> bool:
    """Return True when *line* clearly describes the removal, migration, or
    replacement of a legacy path rather than pointing at it as a live
    reference.

    The line counts as removal context only when it carries a removal
    keyword (``_REMOVAL_KEYWORDS``) AND does NOT carry a live-reference
    indicator (``_LIVE_REFERENCE_INDICATORS``). The double guard keeps
    descriptive lines exempt — e.g. ``stale docs/runbooks/ references
    repointed at docs/ops/`` or ``removed a reference to
    docs/contracts/foo.md`` — while catching live references that merely
    wear a removal adjective, e.g. ``legacy docs/runbooks/foo.md still has
    details``.
    """
    lowered = line.lower()
    if not any(keyword in lowered for keyword in _REMOVAL_KEYWORDS):
        return False
    return not any(indicator in lowered for indicator in _LIVE_REFERENCE_INDICATORS)


class TestNoLegacyPathReferencesInRootConfig:
    """Root-level build/config files must not reference removed doc trees.

    Scope: top-level project metadata files listed in
    ``_ROOT_CONFIG_FILES`` (``pyproject.toml`` today, plus the conventional
    sibling build/config filenames so future additions are covered
    automatically). The ``docs/`` tree is scanned by the classes above;
    this class covers source-of-truth files at the repository root that
    tend to hard-code paths in comments or ``tool.*`` tables.

    Carve-out (narrowed): a line is exempt ONLY when it clearly
    describes the removal or replacement of the legacy tree.
    ``_is_removal_context`` decides this by requiring BOTH (a) a
    removal keyword from ``_REMOVAL_KEYWORDS`` AND (b) the absence of
    any live-reference indicator from ``_LIVE_REFERENCE_INDICATORS``.

    Exempt (removal context — keyword present, no live indicator)::

        # the old docs/contracts/ tree was removed; use docs/spec/
        # docs/runbooks/ references were repointed at docs/ops/
        # no durable replacement for docs/contracts/x.md

    Flagged (live reference — either a live indicator is present next
    to a removal adjective, or no removal keyword exists at all)::

        # see docs/contracts/25-matrix-e2ee-readiness.md
        # docs/runbooks/foo.md is in this repo
        # refer to docs/contracts/bar.md for details

    ``.git/`` is never scanned by this class.
    """

    @pytest.mark.parametrize(
        "filename",
        _ROOT_CONFIG_FILES,
    )
    def test_no_legacy_path_references(self, filename: str) -> None:
        filepath = _ROOT / filename
        if not filepath.is_file():
            pytest.skip(f"{filename} not present at repository root")

        text = filepath.read_text(encoding="utf-8")
        failures: list[str] = []

        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_removal_context(line):
                continue
            for match in _LEGACY_PROSE_RE.finditer(line):
                failures.append(
                    f"{filename}:{lineno}: "
                    f"reference to legacy path '{match.group()}'"
                )

        if failures:
            pytest.fail(
                f"Found {len(failures)} reference(s) to removed doc trees "
                f"in {filename} (use docs/spec/, docs/ops/, or docs/dev/ "
                f"instead):\n  " + "\n  ".join(failures)
            )


# ===========================================================================
# 4. Test paths referenced in docs must resolve (or be allow-listed)
# ===========================================================================


# Match a tests/-prefixed Python path appearing in prose, e.g.
# ``tests/test_foo.py`` or ``tests/helpers/bar.py``.
_TEST_PATH_RE = re.compile(r"tests/[a-zA-Z0-9_/.-]+\.py")

# Narrow historical allow-list. Each entry is a (doc_relative_path, regex,
# reason) tuple. A flagged line is exempt only when BOTH hold: it lives in
# the named file AND the regex matches the line. Regexes must be tight
# enough to scope the exemption to the historical context, not arbitrary
# surrounding prose. Add an entry only when a doc deliberately references a
# non-existent test file as historical/triage context (the surrounding
# section must itself say so).
_HISTORICAL_TEST_PATH_ALLOWLIST: list[tuple[str, "re.Pattern[str]", str]] = [
    # docs/dev/TESTING_GUIDE.md references meshtastic-matrix-relay's own
    # `tests/sqlite_provenance.py` as an external sibling-repo example,
    # not a MEDRE file. The regex anchors on the same-line "external
    # sibling repo" label so a future MEDRE-local reference cannot
    # inherit the exemption.
    (
        "docs/dev/TESTING_GUIDE.md",
        re.compile(r"tests/sqlite_provenance\.py.*external sibling repo"),
        "External-repo reference: the path belongs to the "
        "meshtastic-matrix-relay sibling project, not MEDRE.",
    ),
    # docs/dev/runtime-execution-authority-audit.md has a section
    # "Missing tests/test_replay_delivery.py -- triaged: no gap, no file
    # needed" that explicitly documents why this file does not and should
    # not exist; coverage lives in sibling files. All four mentions live
    # inside that triage section. The regex requires a same-line absence
    # marker (Missing / no dedicated / Do not create before the path, or
    # triaged / needed / finding after it) so a future live reference
    # such as "Run tests/test_replay_delivery.py" cannot inherit the
    # exemption.
    (
        "docs/dev/runtime-execution-authority-audit.md",
        re.compile(
            r"(?:"
            r"\b(?:Missing|no dedicated|Do not create)\b"
            r".*tests/test_replay_delivery\.py"
            r"|"
            r"tests/test_replay_delivery\.py"
            r".*\b(?:triaged|needed|finding)\b"
            r")"
        ),
        "Triaged historical section: explains why this file does not and "
        "should not exist; coverage lives in sibling files.",
    ),
    # docs/dev/testing.md "Completed Splits" table. Each row names a
    # former test file alongside a "Split" or "Deleted" Result column.
    # The exemption regex requires BOTH the path AND the Result marker on
    # the same line, so a live reference (no marker) cannot inherit the
    # exemption.
    *[
        (
            "docs/dev/testing.md",
            re.compile(rf"`{re.escape(name)}`.*\bSplit\b"),
            f"Historical 'Completed Splits' table row: {name} was split "
            "into behavioral-domain files.",
        )
        for name in (
            "tests/test_adapter_callback_bridge.py",
            "tests/test_longrun_callback_bridge.py",
            "tests/test_operator_workflows.py",
            "tests/test_pipeline.py",
            "tests/test_replay.py",
            "tests/test_cli.py",
            "tests/test_docker_bridge_artifacts.py",
        )
    ],
    (
        "docs/dev/testing.md",
        re.compile(r"`tests/test_storage_outbox\.py`.*\bDeleted\b"),
        "Historical 'Completed Splits' table row: test_storage_outbox.py "
        "was deleted (split into 5 behavioral-domain files).",
    ),
]


class TestDocTestReferencesResolve:
    """Test paths mentioned in docs/*.md must resolve to a real file under
    ``tests/`` or be on the narrow historical allow-list.

    Catches the bug class where a doc references a renamed, deleted, or
    never-created test file as if it currently exists. Historical
    references that explicitly describe a missing file (e.g. a triage
    section) are exempt via ``_HISTORICAL_TEST_PATH_ALLOWLIST``.

    The guard intentionally scans every line, including lines inside
    fenced code blocks: a ``tests/*.py`` reference that does not resolve
    is a bug class regardless of whether it appears in prose or in a
    code example. Fence-skipping is not added because the tighter
    behavior has not produced false positives.
    """

    @pytest.mark.parametrize(
        "filepath",
        _all_md_files(),
        ids=lambda p: str(p.relative_to(_ROOT)),
    )
    def test_referenced_test_files_exist(self, filepath: Path) -> None:
        rel = _relative(filepath)
        text = filepath.read_text(encoding="utf-8")

        applicable_allow_regexes = [
            regex
            for doc_path, regex, _reason in _HISTORICAL_TEST_PATH_ALLOWLIST
            if doc_path == rel
        ]

        failures: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _TEST_PATH_RE.finditer(line):
                test_path = match.group()
                test_file = _ROOT / test_path
                if test_file.exists():
                    continue
                if any(regex.search(line) for regex in applicable_allow_regexes):
                    continue
                failures.append(
                    f"{rel}:{lineno}: references '{test_path}' "
                    "which does not exist"
                )

        if failures:
            pytest.fail(
                "Docs reference test files that do not exist. Rename the "
                "reference, remove it, or label historical and add an entry "
                "to _HISTORICAL_TEST_PATH_ALLOWLIST in "
                "tests/test_docs_links.py:\n  " + "\n  ".join(failures)
            )


# ===========================================================================
# 5. Removal-context carve-out boundary (locks the narrowed rule above)
# ===========================================================================


class TestRemovalContextCarveOut:
    """Lock the narrowed removal-context carve-out so a live reference
    wearing a removal adjective is still flagged.

    The cases below document the boundary between exempt removal context
    (``_is_removal_context`` -> True) and flagged live references
    (``_is_removal_context`` -> False). They exist because the previous
    broad keyword-anywhere rule let lines like ``legacy docs/runbooks/foo.md
    still has details`` slip through as exempt. Add a case here whenever
    the carve-out semantics change.
    """

    @pytest.mark.parametrize(
        "line,expected",
        [
            # --- Exempt: genuine removal context (keyword + no live indicator)
            (
                "stale docs/runbooks/ references repointed at docs/ops/",
                True,
            ),
            ("removed a reference to docs/contracts/foo.md", True),
            ("docs/contracts/x.md (no durable replacement)", True),
            ("replaced docs/contracts/ old tree with docs/spec/", True),
            ("migrated docs/runbooks/foo.md to docs/ops/foo.md", True),
            ("# docs/contracts/ removed; use docs/spec/ instead", True),
            (
                "prevents docs/contracts/ and docs/runbooks/ references "
                "from returning",
                True,
            ),
            # --- Flagged: live reference wearing a removal adjective
            ("legacy docs/runbooks/foo.md still has details", False),
            ("legacy docs/contracts/bar.md still exists", False),
            ("former docs/runbooks/baz.md has details", False),
            # --- Flagged: pure live reference (no removal keyword at all)
            ("see docs/runbooks/foo.md for details", False),
            ("refer to docs/contracts/bar.md", False),
            ("docs/runbooks/qux.md is in this repo", False),
            ("found in docs/contracts/old.md", False),
            ("documented in docs/runbooks/run.md", False),
        ],
    )
    def test_is_removal_context(self, line: str, expected: bool) -> None:
        assert _is_removal_context(line) is expected
