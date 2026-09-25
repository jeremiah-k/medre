"""Release readiness convergence tests.

Verifies that release-readiness.md, evidence-levels.md, README, authority
maps, schemas, CLI docs, and test filenames are convergent — no drift between
status vocabularies, no stale process language in durable docs, and no
leftover alpha/beta test filenames.
"""

from __future__ import annotations

from functools import cache
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
        "implemented-not-executed",
        "synthetic-tested",
        "docker-validated",
        "local-integration-validated",
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


def test_local_integration_historical_evidence_is_preserved() -> None:
    """Recorded MeshCore/LXMF local-integration evidence keeps its provenance."""
    text = _read(_READINESS)

    matrix_marker = "## 1. Capability Matrix"
    definitions_marker = "## 2. Status Definitions"
    assert matrix_marker in text
    assert definitions_marker in text
    matrix = text.split(matrix_marker, 1)[1].split(definitions_marker, 1)[0]
    rows = [
        line
        for line in matrix.splitlines()
        if line.startswith("| Deterministic local integration")
    ]
    assert len(rows) == 1, "capability matrix must have exactly one integration row"
    cells = [cell.strip() for cell in rows[0].strip("|").split("|")]
    assert len(cells) == 5
    assert cells[0] == "Deterministic local integration"
    assert cells[3] == "local-integration-validated"
    assert cells[4] == "local-integration-validated"

    historical_marker = "### 6.1 Recorded historical evidence (pre-consolidation tree)"
    not_executed_marker = "### 6.2 Not-executed gates (no evidence at any tier)"
    future_marker = "### 6.3 Future release gates (not required for prerelease)"
    assert historical_marker in text
    assert not_executed_marker in text
    assert future_marker in text
    historical = text.split(historical_marker, 1)[1].split(not_executed_marker, 1)[0]
    not_executed = text.split(not_executed_marker, 1)[1].split(future_marker, 1)[0]

    assert "| MeshCore deterministic real-SDK TCP local integration" in historical
    assert "| LXMF process-isolated real RNS/LXMRouter local integration" in historical
    assert "Recorded date: 2026-08-21" in historical
    assert "`ba2bceffad6810855e1858d202aee6039ac49824`" in historical
    assert "Workflow run: `32529498484`" in historical
    assert "`409762d0cbba1d46aab1fafb60449eca0370ae00`" in historical
    assert "`5c8a67e922612f18ab01deefaeeb39c429b4df02`" in historical
    assert "MeshCore deterministic local integration" not in not_executed
    assert "LXMF process-isolated local integration" not in not_executed


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


@cache
def _stale_terms_in_docs(subdir: str) -> tuple[tuple[Path, int, str], ...]:
    """Cache immutable spec/ops scans shared by the paired guards below."""
    return tuple(find_stale_terms([subdir], FORBIDDEN_TERMS))


def test_no_stale_process_language_in_spec() -> None:
    """spec/ docs must not contain stale internal process language."""
    raw = _stale_terms_in_docs("spec")
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale process language in spec/:\n" + "\n".join(
        violations
    )


def test_no_stale_process_language_in_ops() -> None:
    """ops/ docs must not contain stale internal process language."""
    raw = _stale_terms_in_docs("ops")
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
    FORBIDDEN_TERMS list defined in tests/helpers/forbidden_terms.py.

    Note: planning-cycle vocabulary (PLANNING_CYCLE_TERMS) is enforced
    separately by the durable-language guard, not by this convergence test."""
    raw = _stale_terms_in_docs("spec")
    violations = [
        f"  {md_file.relative_to(_ROOT)}:{lineno}: '{content}'"
        for md_file, lineno, content in raw
    ]
    assert not violations, "Found stale alpha/beta branding in spec/:\n" + "\n".join(
        violations
    )


def test_no_stale_alpha_beta_branding_in_ops() -> None:
    """ops/ docs must not contain stale alpha/beta branding terms."""
    raw = _stale_terms_in_docs("ops")
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

    All 'TestAlpha*' classes are stale naming from the prerelease branding
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
