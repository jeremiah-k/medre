"""Architecture guard tests for the convergence diagnostics package.

Ensures the decomposed convergence package structure is intact, the former
monolith is gone, __init__.py is documentation-only (no re-exports, no
__all__), and cross-populated fields (orphan_count) are correctly wired.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"
_CONVERGENCE_MONOLITH = _SRC / "core" / "diagnostics" / "convergence.py"
_CONVERGENCE_PKG = _SRC / "core" / "diagnostics" / "convergence"
_CONVERGENCE_INIT = _CONVERGENCE_PKG / "__init__.py"


class TestConvergenceMonolithRemoved:
    """The former convergence.py monolith must not exist."""

    def test_monolith_file_does_not_exist(self) -> None:
        assert not _CONVERGENCE_MONOLITH.is_file(), (
            "Monolith convergence.py must be deleted; "
            f"found at {_CONVERGENCE_MONOLITH}"
        )


class TestConvergencePackageStructure:
    """The convergence package directory must exist with no re-exports."""

    def test_package_directory_exists(self) -> None:
        assert (
            _CONVERGENCE_PKG.is_dir()
        ), f"Convergence package directory missing: {_CONVERGENCE_PKG}"

    def test_init_exists(self) -> None:
        assert _CONVERGENCE_INIT.is_file(), f"__init__.py missing: {_CONVERGENCE_INIT}"

    def test_init_no_star_re_exports(self) -> None:
        source = _CONVERGENCE_INIT.read_text(encoding="utf-8")
        assert (
            "import *" not in source
        ), "__init__.py must not contain blanket star imports (import *)"

    def test_init_no_import_statements(self) -> None:
        """__init__.py must be documentation-only: no 'from .xxx import' lines."""
        source = _CONVERGENCE_INIT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                pytest.fail(
                    f"__init__.py must not contain import statements; "
                    f"found import at line {node.lineno}"
                )

    def test_init_no_dunder_all(self) -> None:
        """__init__.py must not define __all__."""
        source = _CONVERGENCE_INIT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        pytest.fail("__init__.py must not define __all__")


class TestConvergenceCanonicalImports:
    """All public symbols must be importable from canonical submodules."""

    def test_types_imports(self) -> None:
        from medre.core.diagnostics.convergence.types import (
            ConvergenceSeverity,
            ConvergenceSummary,
            DeliveryTargetConvergence,
            OrphanFinding,
            OrphanReport,
        )

        assert ConvergenceSeverity is not None
        assert ConvergenceSummary is not None
        assert DeliveryTargetConvergence is not None
        assert OrphanFinding is not None
        assert OrphanReport is not None

    def test_build_functions_importable(self) -> None:
        from medre.core.diagnostics.convergence.orphans import build_orphan_report
        from medre.core.diagnostics.convergence.summary import (
            build_convergence_summary,
        )

        assert callable(build_convergence_summary)
        assert callable(build_orphan_report)

    def test_kind_constants_importable(self) -> None:
        from medre.core.diagnostics.convergence.types import (
            KIND_ORPHANED_OUTBOX,
        )

        assert KIND_ORPHANED_OUTBOX == "orphaned_outbox"

    def test_facade_re_exports_do_not_work(self) -> None:
        """Importing re-exported symbols from the package root raises AttributeError.

        The package itself is always importable (that's just a Python package),
        but accessing re-exported symbols on it must fail since __init__.py
        no longer defines them.
        """
        import medre.core.diagnostics.convergence as pkg

        facade_names = [
            "ConvergenceSeverity",
            "DeliveryTargetConvergence",
            "ConvergenceSummary",
            "build_convergence_summary",
            "OrphanFinding",
            "OrphanReport",
            "build_orphan_report",
        ]
        for name in facade_names:
            assert not hasattr(pkg, name), (
                f"convergence package must not re-export {name!r}; "
                f"use canonical submodule imports instead"
            )


class TestOrphanCountCrossPopulated:
    """Structural guarantees for orphan_count cross-population.

    The real EvidenceCollector integration path (collector →
    build_convergence_summary → build_orphan_report → cross-populate
    orphan_count) is covered by the collector's own test suite.  These
    structural tests verify the contract the collector relies on.
    """

    def test_orphan_count_key_exists_in_to_dict(self) -> None:
        """``orphan_count`` key must be present in ConvergenceSummary.to_dict()."""
        from medre.core.diagnostics.convergence.summary import (
            build_convergence_summary,
        )

        summary = build_convergence_summary()
        result = summary.to_dict()
        assert "orphan_count" in result, (
            "ConvergenceSummary.to_dict() must include 'orphan_count' key; "
            f"found keys: {sorted(result.keys())}"
        )

    def test_orphan_count_initial_value_is_none(self) -> None:
        """Standalone convergence summary must have orphan_count=None before cross-population."""
        from medre.core.diagnostics.convergence.summary import (
            build_convergence_summary,
        )

        summary = build_convergence_summary()
        assert summary.orphan_count is None, (
            "Standalone ConvergenceSummary.orphan_count must be None "
            "before EvidenceCollector cross-populates it"
        )

    def test_to_dict_returns_mutable_regular_dict(self) -> None:
        """to_dict() must return a regular mutable dict (not a frozen/MappingProxy)."""
        from medre.core.diagnostics.convergence.summary import (
            build_convergence_summary,
        )

        summary = build_convergence_summary()
        result = summary.to_dict()
        assert isinstance(
            result, dict
        ), f"to_dict() must return dict, got {type(result).__name__}"
        # Verify mutability — the collector patches orphan_count in-place.
        original = result["orphan_count"]
        result["orphan_count"] = 42
        assert result["orphan_count"] == 42, (
            "to_dict() result must be a mutable dict so the collector can "
            "cross-populate orphan_count"
        )
        result["orphan_count"] = original  # restore for hygiene
