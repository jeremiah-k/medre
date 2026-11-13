"""Guard banning self-compatibility retention vocabulary from ``src/``.

``src/`` docstrings and comments may not present MEDRE's own surfaces as
retained compatibility baggage. The phrase family compiled in
``tests/helpers/forbidden_terms.py`` (``SELF_COMPAT_TERMS``) bans shim
branding, compatibility-branded surfaces, legacy-branded aliases, wrappers,
and signatures, and prose asserting that some prior spelling of a name
still works. The guard scans Python files under ``src/`` only and fails on
any match the allowlist does not excuse.

Scope: ``src/`` ONLY. The generic durable-language families keep their
existing docs/tests/examples scope; this family never joins
``ALL_FORBIDDEN_PATTERNS``, so no other enforcer's behavior changes.

Foreign contracts: an exception may live only in
``tests/helpers/self_compat_keep_paths.py`` as a tight path-prefix and
phrase pair naming the foreign contract it excuses. The allowlist is
self-asserting — an entry whose allowlisted text no longer exists fails
this guard, so stale exceptions cannot accumulate silently.

The shipped allowlist is empty: ``src/`` scans clean at these phrases. The
machinery is exercised with synthetic entries against a synthetic tree, and
every fixture string below is assembled from fragments so this file, like
the helpers it imports, contains no blocked phrase as a literal substring.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers.forbidden_terms import (
    SELF_COMPAT_TERMS,
    find_forbidden_in_tree,
)
from tests.helpers.self_compat_keep_paths import (
    SELF_COMPAT_KEEP_PATHS,
    SelfCompatKeep,
    excusing_contract,
    stale_entries,
)

_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _ROOT / "src"

# ---------------------------------------------------------------------------
# Synthetic fixtures (fragment-assembled; never a literal banned phrase)
# ---------------------------------------------------------------------------

_SYNTHETIC_LINE = "# Quoted vendor wording: " + "kept " + "for compatibility"
_KEEP_PATTERN = re.compile(
    r"\bkept " + r"for (?:compatibility|older|legacy|existing callers?)\b",
    re.IGNORECASE,
)
_FOREIGN_CONTRACT = "foreign SDK wire contract"


def _write_synthetic_tree(root: Path) -> Path:
    """Build a minimal ``src``-shaped tree carrying the synthetic phrase.

    The phrase lands in an allowlistable adapter file, in an unprefixed core
    file, and in a non-Python file the guard's ``.py``-only scope ignores.
    """
    src = root / "src"
    adapter_dir = src / "medre" / "adapters"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "foreign_sdk.py").write_text(
        _SYNTHETIC_LINE + "\n",
        encoding="utf-8",
    )
    (src / "medre" / "core.py").parent.mkdir(parents=True, exist_ok=True)
    (src / "medre" / "core.py").write_text(
        _SYNTHETIC_LINE + "\n",
        encoding="utf-8",
    )
    (src / "medre" / "plain.py").write_text(
        "# current-contract prose\n",
        encoding="utf-8",
    )
    (adapter_dir / "README.md").write_text(
        _SYNTHETIC_LINE + "\n",
        encoding="utf-8",
    )
    return src


def _scan_violations(src: Path, entries: tuple[SelfCompatKeep, ...]) -> list[str]:
    """Run the guard scan the way the enforcer test does and report misses."""
    raw = find_forbidden_in_tree([src], SELF_COMPAT_TERMS, extensions=(".py",))
    return [
        f"  {path.relative_to(src).as_posix()}:{lineno}: '{line}'"
        for path, lineno, line in raw
        if excusing_contract(path.relative_to(src).as_posix(), line, entries) is None
    ]


class TestSrcSelfCompatVocabulary:
    """``src/`` must stay free of self-compatibility retention phrases."""

    def test_src_python_files_scan_clean(self) -> None:
        """No banned phrase appears in src/ .py files outside the allowlist.

        Reports each violation with path, line number, and content. A hit
        under an allowlisted prefix whose line also matches the entry's
        pattern is excused; every other hit fails the guard.
        """
        if not _SRC_DIR.is_dir():
            pytest.skip("src/ not found")

        raw = find_forbidden_in_tree([_SRC_DIR], SELF_COMPAT_TERMS, extensions=(".py",))
        violations = [
            f"  {path.relative_to(_ROOT).as_posix()}:{lineno}: '{line}'"
            for path, lineno, line in raw
            if excusing_contract(
                path.relative_to(_SRC_DIR).as_posix(), line, SELF_COMPAT_KEEP_PATHS
            )
            is None
        ]

        if violations:
            pytest.fail(
                "Found self-compatibility retention vocabulary in src/ "
                "Python files. Surfaces must be described by their current "
                "contract, not as baggage kept around for earlier callers:\n"
                + "\n".join(violations)
            )

    def test_allowlist_entries_reference_live_text(self) -> None:
        """Every allowlist entry's text still exists under its prefix.

        The allowlist is self-asserting: an entry whose phrase no longer
        appears under its path prefix is stale and fails here, so foreign
        contract exceptions die loudly when their wording disappears.
        """
        stale = stale_entries([_SRC_DIR])
        if stale:
            pytest.fail(
                "Stale self-compatibility allowlist entries — the allowlisted "
                "text no longer exists; delete the entry:\n"
                + "\n".join(
                    f"  prefix={entry.path_prefix} contract={entry.contract!r}"
                    for entry in stale
                )
            )


class TestAllowlistMachinery:
    """Synthetic-entry proof of both allowlist contract halves.

    The shipped allowlist is empty, so these tests carry the evidence that
    a (path prefix, phrase) pair carves out exactly its matching lines and
    that stale entries are detected, without seeding real exceptions.
    """

    def _entry(self) -> SelfCompatKeep:
        return SelfCompatKeep(
            path_prefix="medre/adapters/foreign_sdk.py",
            pattern=_KEEP_PATTERN,
            contract=_FOREIGN_CONTRACT,
        )

    def test_guard_scan_scope_is_python_only(self, tmp_path: Path) -> None:
        """The guard scans .py files; sibling non-Python files are out of scope."""
        src = _write_synthetic_tree(tmp_path)
        raw = find_forbidden_in_tree([src], SELF_COMPAT_TERMS, extensions=(".py",))
        assert {path.name for path, _lineno, _line in raw} == {
            "foreign_sdk.py",
            "core.py",
        }

    def test_tight_pair_excuses_only_matching_path_and_line(
        self, tmp_path: Path
    ) -> None:
        """A prefix/phrase pair carves only its own file's matching line."""
        entry = self._entry()

        # Exact prefixed file with the matching line: excused.
        assert (
            excusing_contract(
                "medre/adapters/foreign_sdk.py", _SYNTHETIC_LINE, (entry,)
            )
            == _FOREIGN_CONTRACT
        )
        # Same line outside the prefix: not excused.
        assert excusing_contract("medre/core.py", _SYNTHETIC_LINE, (entry,)) is None
        # Sibling file under a directory-style near-miss prefix: not excused.
        assert (
            excusing_contract(
                "medre/adapters/foreign_sdk_extra.py", _SYNTHETIC_LINE, (entry,)
            )
            is None
        )
        # Matching path with a non-matching line: not excused.
        assert (
            excusing_contract(
                "medre/adapters/foreign_sdk.py", "# current-contract prose", (entry,)
            )
            is None
        )

    def test_entry_leaves_unprefixed_hits_failing(self, tmp_path: Path) -> None:
        """With the entry applied, only the unprefixed hit still fails."""
        src = _write_synthetic_tree(tmp_path)
        entries = (self._entry(),)
        violations = _scan_violations(src, entries)
        assert violations == [f"  medre/core.py:1: '{_SYNTHETIC_LINE}'"]
        assert _scan_violations(src, ()) == [
            f"  medre/adapters/foreign_sdk.py:1: '{_SYNTHETIC_LINE}'",
            f"  medre/core.py:1: '{_SYNTHETIC_LINE}'",
        ]

    def test_stale_entry_detected_when_text_missing(self, tmp_path: Path) -> None:
        """Entries whose text vanished — or whose prefix never had it — are stale."""
        src = _write_synthetic_tree(tmp_path)
        entry = self._entry()
        assert stale_entries([src], (entry,)) == []

        empty_tree = tmp_path / "elsewhere"
        empty_tree.mkdir()
        assert stale_entries([empty_tree], (entry,)) == [entry]

        # A prefix naming an existing file without the phrase is stale too.
        moved = replace(entry, path_prefix="medre/plain.py")
        assert stale_entries([src], (moved,)) == [moved]
