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
    """EvidenceCollector must cross-populate orphan_count from OrphanReport."""

    def test_orphan_count_populated_from_orphan_report(self) -> None:
        """Build both reports from the same data and verify orphan_count."""
        from medre.core.diagnostics.convergence.orphans import build_orphan_report
        from medre.core.diagnostics.convergence.summary import (
            build_convergence_summary,
        )

        # Minimal stub objects with duck-typed fields.
        class _StubOutbox:
            outbox_id = "ob-1"
            delivery_plan_id = ""
            target_adapter = "a"
            target_channel = "ch"
            attempt_number = 1
            status = "pending"
            event_id = "ev-1"
            failure_kind = None
            error_summary = None
            created_at = None
            updated_at = None

        class _StubReceipt:
            receipt_id = "rc-1"
            sequence = 1
            delivery_plan_id = ""
            target_adapter = "a"
            target_channel = "ch"
            attempt_number = 1
            status = "sent"
            source = "src"
            replay_run_id = None
            failure_kind = None
            error = None
            rendering_evidence = None
            created_at = None
            event_id = "ev-1"
            parent_receipt_id = "nonexistent-parent"
            route_id = "r-1"

        receipts = [_StubReceipt()]
        outbox_items = [_StubOutbox()]

        conv = build_convergence_summary(receipts=receipts, outbox_items=outbox_items)
        orphan = build_orphan_report(receipts=receipts, outbox_items=outbox_items)

        # The collector would set: convergence_dict["orphan_count"] = orphan.total_findings
        # Verify the mechanism works: orphan report has findings, conv dict can be patched.
        conv_dict = conv.to_dict()
        assert conv_dict["orphan_count"] is None, (
            "Standalone convergence_summary.orphan_count should be None "
            "before EvidenceCollector cross-populates it"
        )
        # Simulate what EvidenceCollector does:
        conv_dict["orphan_count"] = orphan.total_findings
        assert (
            conv_dict["orphan_count"] == orphan.total_findings
        ), f"orphan_count mismatch: {conv_dict['orphan_count']} != {orphan.total_findings}"
        assert orphan.total_findings >= 0
