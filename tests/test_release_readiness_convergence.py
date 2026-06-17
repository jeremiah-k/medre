"""Release readiness convergence tests.

Verifies that release-readiness.md, evidence-levels.md, README, authority
maps, schemas, CLI docs, and test filenames are convergent — no drift between
status vocabularies, no stale process language in durable docs, and no
leftover alpha/beta test filenames.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.forbidden_terms import FORBIDDEN_TERMS, find_stale_terms

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _ROOT / "docs"
_READINESS = _DOCS / "spec" / "appendices" / "release-readiness.md"
_EVIDENCE = _DOCS / "spec" / "appendices" / "evidence-levels.md"
_README = _ROOT / "README.md"
_TESTS = _ROOT / "tests"
_OPS = _DOCS / "ops"
_SCHEMAS = _DOCS / "schemas"


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


# ===========================================================================
# 1. Prerelease / no-public-API commitment
# ===========================================================================


def test_release_readiness_states_prerelease() -> None:
    """release-readiness.md says prerelease and no public API commitment."""
    text = _read(_READINESS).lower()
    assert (
        "pre-release" in text or "prerelease" in text
    ), "release-readiness.md must state pre-release status"
    assert (
        "no public api" in text
    ), "release-readiness.md must state no public API commitment"


def test_readme_has_prerelease_disclaimer() -> None:
    """README has a prerelease disclaimer."""
    text = _read(_README).lower()
    assert (
        "pre-release" in text or "prerelease" in text
    ), "README must contain a prerelease disclaimer"
    assert (
        "no stable public api" in text or "not production-ready" in text
    ), "README must warn about unstable public API or not production-ready status"


# ===========================================================================
# 2. Status vocabulary convergence
# ===========================================================================


def test_release_readiness_uses_synthetic_tested() -> None:
    """release-readiness.md must use 'synthetic-tested', not 'fake-tested'.

    evidence-levels.md defines the canonical capability status vocabulary
    including 'synthetic-tested'. release-readiness.md must use the same
    labels to avoid status-label drift.
    """
    text = _read(_READINESS)
    # fake-tested is the legacy label; synthetic-tested is canonical
    assert "fake-tested" not in text, (
        "release-readiness.md contains 'fake-tested' but should use "
        "'synthetic-tested' per evidence-levels.md"
    )
    assert "synthetic-tested" in text, (
        "release-readiness.md must use 'synthetic-tested' per "
        "evidence-levels.md capability status definitions"
    )


def test_evidence_levels_defines_shared_status_labels() -> None:
    """evidence-levels.md and release-readiness.md share core status labels."""
    ev_text = _read(_EVIDENCE)
    rd_text = _read(_READINESS)

    # These labels must appear in both documents
    shared_labels = [
        "synthetic-tested",
        "docker-validated",
        "live-validated",
    ]
    for label in shared_labels:
        assert f"`{label}`" in ev_text, (
            f"evidence-levels.md must define '{label}' in its capability "
            f"status table"
        )
        assert label in rd_text, (
            f"release-readiness.md must use '{label}' from evidence-levels.md "
            f"vocabulary"
        )


# ===========================================================================
# 3. No alpha/beta test filenames
# ===========================================================================


def test_no_alpha_test_files() -> None:
    """No tests/test_alpha_*.py files should remain."""
    matches = sorted(_TESTS.glob("test_alpha_*.py"))
    assert not matches, "Found leftover alpha test files: " + ", ".join(
        m.name for m in matches
    )


def test_no_beta_test_files() -> None:
    """No tests/test_beta_*.py files should remain."""
    matches = sorted(_TESTS.glob("test_beta_*.py"))
    assert not matches, "Found leftover beta test files: " + ", ".join(
        m.name for m in matches
    )


# ===========================================================================
# 4. Authority map — all referenced docs exist
# ===========================================================================

#: Entries from release-readiness.md §6 Authority Domains table.
_AUTHORITY_MAP_ENTRIES: list[tuple[str, str]] = [
    # (spec_page_relative, audit_doc_relative)
    ("docs/spec/delivery-lifecycle.md", "docs/dev/lifecycle-authority-audit.md"),
    ("docs/spec/adapter-runtime.md", "docs/dev/adapter-reality-audit.md"),
    ("docs/spec/event-model.md", "docs/dev/conversation-graph-audit.md"),
    ("docs/spec/routing-delivery.md", "docs/dev/planning-authority-audit.md"),
    ("docs/spec/diagnostics-evidence.md", "docs/dev/operator-surface-audit.md"),
    ("docs/spec/storage.md", "docs/dev/persistence-authority-audit.md"),
    # Runtime execution has no spec page; audit is interim
    ("", "docs/dev/runtime-execution-authority-audit.md"),
    (
        "docs/spec/diagnostics-evidence.md",
        "docs/dev/runtime-evidence-completeness-audit.md",
    ),
]


@pytest.mark.parametrize(
    ("spec_page", "audit_doc"),
    _AUTHORITY_MAP_ENTRIES,
    ids=lambda v: v if isinstance(v, str) and v else "(none)",
)
def test_authority_map_doc_exists(spec_page: str, audit_doc: str) -> None:
    """Each authority-map entry references a file that exists on disk."""
    if spec_page:
        assert (
            _ROOT / spec_page
        ).is_file(), f"Authority map spec page missing: {spec_page}"
    assert (
        _ROOT / audit_doc
    ).is_file(), f"Authority map audit doc missing: {audit_doc}"


def test_authority_domains_in_release_readiness() -> None:
    """release-readiness.md §6 lists authority domains with spec+audit refs."""
    text = _read(_READINESS)
    # Check the authority domains section exists
    assert (
        "Authority Domains" in text
    ), "release-readiness.md must have an Authority Domains section"
    # Check at least a few known audit docs are referenced
    expected_refs = [
        "lifecycle-authority-audit.md",
        "adapter-reality-audit.md",
        "persistence-authority-audit.md",
    ]
    for ref in expected_refs:
        assert ref in text, f"release-readiness.md authority table must reference {ref}"


# ===========================================================================
# 5. No stale recovered_status in schemas/examples
# ===========================================================================


def test_schemas_use_observed_status_not_recovered() -> None:
    """Schemas and examples must use 'observed_status', not 'recovered_status'.

    'recovered_status' is a stale field name. The current canonical field
    is 'observed_status'.
    """
    schema_files = sorted(_SCHEMAS.rglob("*.json"))
    violations: list[str] = []
    for sf in schema_files:
        text = _read(sf)
        if "recovered_status" in text:
            violations.append(f"{sf.relative_to(_ROOT)}: contains 'recovered_status'")
    assert not violations, (
        "Schemas/examples contain stale 'recovered_status' (use "
        "'observed_status' instead):\n" + "\n".join(violations)
    )


# ===========================================================================
# 6. CLI docs distinguish --config vs --storage-path
# ===========================================================================


def test_ops_docs_show_config_and_storage_path_distinction() -> None:
    """Ops docs must show both --config and --storage-path with distinct roles.

    --config is for runtime/replay commands that need route resolution.
    --storage-path is for read-only inspect commands that access SQLite
    directly without a config file.
    """
    # Check configuration.md for the distinction
    config_doc = _OPS / "configuration.md"
    if not config_doc.is_file():
        pytest.skip("docs/ops/configuration.md not found")

    text = _read(config_doc)
    assert "--config" in text, "configuration.md must document --config"
    assert "--storage-path" in text, "configuration.md must document --storage-path"
    # Verify they are described with different scopes
    text_lower = text.lower()
    assert (
        "read-only" in text_lower or "readonly" in text_lower
    ), "configuration.md must describe --storage-path as read-only"


# ===========================================================================
# 7. No stale process language in spec/ops
# ===========================================================================


def test_no_stale_process_language_in_spec() -> None:
    """spec/ docs must not contain stale internal process language."""
    raw = find_stale_terms(["spec"], FORBIDDEN_TERMS)
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale process language in spec/:\n" + "\n".join(
        violations
    )


def test_no_stale_process_language_in_ops() -> None:
    """ops/ docs must not contain stale internal process language."""
    raw = find_stale_terms(["ops"], FORBIDDEN_TERMS)
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale process language in ops/:\n" + "\n".join(
        violations
    )


# ===========================================================================
# 8. Durable docs free of stale alpha/beta branding
# ===========================================================================


def test_no_stale_alpha_beta_branding_in_spec() -> None:
    """spec/ docs must not contain stale alpha/beta branding terms from the
    forbidden planning-cycle vocabulary defined in tests/helpers/forbidden_terms.py."""
    raw = find_stale_terms(["spec"], FORBIDDEN_TERMS)
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale alpha/beta branding in spec/:\n" + "\n".join(
        violations
    )


def test_no_stale_alpha_beta_branding_in_ops() -> None:
    """ops/ docs must not contain stale alpha/beta branding terms."""
    raw = find_stale_terms(["ops"], FORBIDDEN_TERMS)
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale alpha/beta branding in ops/:\n" + "\n".join(
        violations
    )


# ===========================================================================
# 9. No TestAlpha class names in test suite
# ===========================================================================


def test_no_test_alpha_class_names() -> None:
    """No test class name should start with 'TestAlpha'.

    All 'TestAlpha*' classes are stale naming from the alpha walkthrough
    era. They must be renamed to drop the 'Alpha' qualifier.
    """
    import ast as _ast

    violations: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = _ast.parse(source)
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name.startswith("TestAlpha"):
                violations.append(
                    f"  {path.relative_to(_ROOT)}:{node.lineno}: " f"class {node.name}"
                )
    assert not violations, (
        "Found test classes starting with 'TestAlpha'. "
        "Rename them to drop the 'Alpha' qualifier:\n" + "\n".join(violations)
    )


# ===========================================================================
# 10. Historical monolith references include 'Former'
# ===========================================================================


def test_deleted_monolith_refs_include_former() -> None:
    """References to deleted monoliths in docs must include 'Former' to mark
    them as historical.

    The DELETED_MONOLITHS list in test_test_suite_structure.py tracks files
    that were split and deleted. Any reference to these filenames (with .py
    extension) in docs/ must appear alongside 'Former' (case-insensitive)
    to avoid confusion with currently-existing files.
    """
    # Same list as test_test_suite_structure.DELETED_MONOLITHS — kept in sync
    # manually to avoid cross-module test imports.
    _DELETED_MONOLITHS = (
        "test_adapter_callback_bridge",
        "test_longrun_callback_bridge",
        "test_operator_workflows",
        "test_pipeline",
        "test_replay",
        "test_cli",
        "test_alpha_walkthrough_cli",
        "test_docker_bridge_artifacts",
    )

    # Search durable docs for monolith file paths without 'Former' context.
    violations: list[str] = []
    for md_file in sorted(_DOCS.rglob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for monolith in _DELETED_MONOLITHS:
                # Match the monolith as an explicit file reference (with .py)
                monolith_file = f"{monolith}.py"
                if monolith_file not in line:
                    continue
                # Allow if the line already says "Former" (case-insensitive)
                if "former" in line.lower():
                    continue
                violations.append(
                    f"  {md_file.relative_to(_ROOT)}:{lineno}: "
                    f"'{monolith_file}' without 'Former'"
                )
    assert (
        not violations
    ), "References to deleted monolith files must include 'Former':\n" + "\n".join(
        violations
    )
