"""Durable-language guard tests.

Asserts that internal planning-cycle vocabulary does not appear in any
durable artifact. Terms like "tranche", "boulder", "sprint", and other
internal process labels are planning artifacts and MUST NOT appear in:

    - documentation under ``docs/`` (spec, ops, dev, schemas, changes)
    - source code comments and docstrings under ``src/``
    - test names, test methods, test comments, test docstrings under ``tests/``
    - example configs and scripts under ``examples/``
    - filenames within any of the above trees

Scan scope (see ``tests/helpers/forbidden_terms.py`` for the shared policy):
    ``docs/``, ``src/``, ``tests/``, ``examples/`` by default.

Not scanned:
    - ``.git/`` — historical git commit messages are preserved; the policy
      does not rewrite history. New commit messages must still comply.
    - ``__pycache__/``, build artifacts, virtualenvs.

Definitional carve-out:
    The forbidden WORDS legitimately live in
    ``tests/helpers/forbidden_terms.py`` (pattern definitions) and in
    ``docs/dev/documentation-style.md`` (the style guide that documents the
    ban by enumerating the terms). This enforcer test also names the terms.
    These three files are exempt from content scans via
    ``DEFINITIONAL_EXEMPT_FILES`` so they do not flag themselves.

If this test fails in files owned by another work track, do NOT weaken it.
Report the violation so the orchestrator can route a cleanup wave.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.forbidden_terms import (
    ALL_FORBIDDEN_PATTERNS,
    DEFINITIONAL_EXEMPT_FILES,
    find_forbidden_in_filenames,
    find_forbidden_in_tree,
    find_stale_terms,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
_DOCS_DIR = _ROOT / "docs"

#: Top-level trees scanned for durable-language violations.
_SCANNED_TREES: list[Path] = [
    _ROOT / "docs",
    _ROOT / "src",
    _ROOT / "tests",
    _ROOT / "examples",
]


class TestNoInternalPlanningLanguage:
    """Internal planning-cycle terms must not appear in any durable artifact.

    Content is scanned in docs/, src/, tests/, and examples/. Filenames are
    scanned separately so forbidden words embedded in file or directory names
    surface even when the file itself has no matching content.
    """

    @pytest.mark.parametrize(
        "scan_dir",
        [_DOCS_DIR / "spec", _DOCS_DIR / "ops", _DOCS_DIR / "dev"],
        ids=lambda d: d.name,
    )
    def test_no_planning_terms_in_core_docs(self, scan_dir: Path) -> None:
        """Spec/ops/dev docs must not contain planning-cycle vocabulary.

        Reports the file path and line number of each violation. Style guide
        files that mention terms in a "do not use" context are exempt via
        ``DEFINITIONAL_EXEMPT_FILES``.
        """
        if not scan_dir.is_dir():
            pytest.skip(f"Directory not found: {scan_dir.relative_to(_ROOT)}")

        raw = find_stale_terms([scan_dir.name], ALL_FORBIDDEN_PATTERNS)
        violations = [
            f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
            for md_file, lineno, content in raw
            if md_file.name not in DEFINITIONAL_EXEMPT_FILES
        ]

        if violations:
            pytest.fail(
                f"Found internal planning-cycle vocabulary in "
                f"{scan_dir.relative_to(_ROOT)}/. These terms are forbidden "
                f"in all durable artifacts:\n" + "\n".join(violations)
            )

    @pytest.mark.parametrize(
        "scan_dir",
        [_DOCS_DIR / "changes", _DOCS_DIR / "schemas"],
        ids=lambda d: d.name,
    )
    def test_no_planning_terms_in_other_docs(self, scan_dir: Path) -> None:
        """Change fragments and schemas must not contain planning vocabulary.

        Under the stricter durable-language policy, ``docs/changes/`` is no
        longer an allowlisted location for internal planning terms. Historical
        fragments that predate the ban are expected to be cleaned up.
        """
        if not scan_dir.is_dir():
            pytest.skip(f"Directory not found: {scan_dir.relative_to(_ROOT)}")

        raw = find_forbidden_in_tree([scan_dir], ALL_FORBIDDEN_PATTERNS)
        violations = [
            f"  {path.relative_to(_ROOT)}:{lineno}: '{content}'"
            for path, lineno, content in raw
        ]

        if violations:
            pytest.fail(
                f"Found internal planning-cycle vocabulary in "
                f"{scan_dir.relative_to(_ROOT)}/. These terms are forbidden "
                f"in all durable artifacts, including change fragments:\n"
                + "\n".join(violations)
            )

    @pytest.mark.parametrize(
        "tree",
        _SCANNED_TREES,
        ids=lambda t: t.name,
    )
    def test_no_planning_terms_in_tree_contents(self, tree: Path) -> None:
        """Source, test, and example file contents must not contain planning terms.

        Scans comments, docstrings, and any other text in ``.py``, ``.md``,
        ``.rst``, ``.toml``, ``.yaml``, ``.yml``, ``.json``, ``.txt``, ``.cfg``,
        and ``.ini`` files. Definitional files
        (``forbidden_terms.py``, this enforcer, ``documentation-style.md``)
        are exempt.
        """
        if not tree.is_dir():
            pytest.skip(f"Directory not found: {tree.relative_to(_ROOT)}")

        raw = find_forbidden_in_tree([tree], ALL_FORBIDDEN_PATTERNS)
        violations = [
            f"  {path.relative_to(_ROOT)}:{lineno}: '{content}'"
            for path, lineno, content in raw
        ]

        if violations:
            pytest.fail(
                f"Found internal planning-cycle vocabulary in "
                f"{tree.relative_to(_ROOT)}/ file contents. These terms are "
                f"forbidden in durable artifacts (code comments, docstrings, "
                f"test names, config):\n" + "\n".join(violations)
            )

    @pytest.mark.parametrize(
        "tree",
        _SCANNED_TREES,
        ids=lambda t: t.name,
    )
    def test_no_planning_terms_in_filenames(self, tree: Path) -> None:
        r"""File and directory names must not contain planning-cycle vocabulary.

        Catches forbidden words embedded in filenames even when surrounding
        content is otherwise clean. The ``tranche`` pattern is a bare
        case-insensitive substring (no word boundaries), so it matches
        digit- and underscore-suffixed forms such as ``tranche6`` or
        ``_tranche_`` that a ``\\btranche\\b`` anchor would miss. The
        previously forbidden name ``test_lxmf_session_tranche6.py`` is
        now ``test_lxmf_session_callback_guards.py`` — the success case
        this enforcer protects against regressing.
        """
        if not tree.is_dir():
            pytest.skip(f"Directory not found: {tree.relative_to(_ROOT)}")

        raw = find_forbidden_in_filenames([tree], ALL_FORBIDDEN_PATTERNS)
        violations = [f"  {rel}" for _path, rel in raw]

        if violations:
            pytest.fail(
                f"Found internal planning-cycle vocabulary in file/directory "
                f"names under {tree.relative_to(_ROOT)}/. Rename the "
                f"offending paths to durable names:\n" + "\n".join(violations)
            )

    def test_changes_directory_no_longer_allowlisted(self) -> None:
        """docs/changes/ is no longer an allowlisted location.

        This test serves as documentation: the previous policy permitted
        planning vocabulary in ``docs/changes/`` because change fragments
        were treated as historical record. Under the stricter durable-language
        policy, change fragments are durable artifacts and must use durable
        vocabulary. The content scan in
        ``test_no_planning_terms_in_other_docs`` enforces this.
        """
        changes_dir = _DOCS_DIR / "changes"
        if not changes_dir.is_dir():
            pytest.skip("docs/changes/ not found")
        # No assertion — the parametrized content scan above enforces the ban.
