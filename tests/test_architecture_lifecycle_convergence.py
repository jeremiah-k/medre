"""Architecture guard tests for the lifecycle convergence diagnostics module.

Ensures the lifecycle_convergence submodule is structurally sound:
- ``lifecycle_convergence.py`` exists inside the convergence package.
- ``build_lifecycle_convergence_findings`` is importable from the canonical
  submodule only (not from the package root).
- All 9 lifecycle finding-kind constants are importable from ``types.py``.
- The convergence ``__init__.py`` remains documentation-only (no re-exports).
- The module source contains no imports from storage, runtime, adapters, or
  evidence — it is pure and read-only.
- The public function accepts generator inputs (``Iterable`` parameters).
- The module performs no write / mutation API calls.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"
_CONVERGENCE_PKG = _SRC / "core" / "diagnostics" / "convergence"
_LIFECYCLE_MODULE = _CONVERGENCE_PKG / "lifecycle_convergence.py"
_CONVERGENCE_INIT = _CONVERGENCE_PKG / "__init__.py"

# The 9 lifecycle finding-kind constants that must exist in types.py
_LIFECYCLE_KINDS = [
    "KIND_TERMINAL_RECEIPT_NONTERMINAL_OUTBOX",
    "KIND_TERMINAL_OUTBOX_NONTERMINAL_RECEIPT",
    "KIND_RECEIPT_OUTBOX_MISMATCH",
    "KIND_RETRY_WAIT_MISSING_NEXT_RETRY",
    "KIND_NEXT_RETRY_IN_PAST",
    "KIND_RETRYABLE_WITHOUT_RETRY_METADATA",
    "KIND_STALLED_DELIVERY_PLAN",
    "KIND_ATTEMPT_COUNT_REGRESSION",
    "KIND_RECEIPT_SEQUENCE_GAP",
]

# Forbidden import substrings — lifecycle_convergence must not reach into
# infrastructure layers.
_FORBIDDEN_IMPORT_PARTS = (
    "storage",
    "runtime",
    "adapters",
    "evidence",
    "database",
    "sqlalchemy",
    "redis",
)

# Forbidden callable names that imply mutation / side effects.
_FORBIDDEN_CALL_NAMES = frozenset(
    {
        "write",
        "save",
        "commit",
        "flush",
        "delete",
        "insert",
        "update",
        "create",
        "execute",
        "put",
        "post",
        "patch",
        "remove",
    }
)


# ---------------------------------------------------------------------------
# Module existence
# ---------------------------------------------------------------------------


class TestModuleExists:
    """The lifecycle_convergence module file must exist."""

    def test_module_file_exists(self) -> None:
        assert (
            _LIFECYCLE_MODULE.is_file()
        ), f"lifecycle_convergence.py missing: {_LIFECYCLE_MODULE}"


# ---------------------------------------------------------------------------
# Canonical imports — public symbols
# ---------------------------------------------------------------------------


class TestCanonicalImports:
    """Public symbols must be importable from canonical submodules only."""

    def test_build_function_importable(self) -> None:
        from medre.core.diagnostics.convergence.lifecycle_convergence import (
            build_lifecycle_convergence_findings,
        )

        assert callable(build_lifecycle_convergence_findings)

    def test_build_function_not_re_exported_from_package_root(self) -> None:
        """build_lifecycle_convergence_findings must NOT be on the package."""
        import medre.core.diagnostics.convergence as pkg

        assert not hasattr(pkg, "build_lifecycle_convergence_findings"), (
            "convergence package must not re-export build_lifecycle_convergence_findings; "
            "use canonical submodule import instead"
        )

    def test_all_nine_kind_constants_importable(self) -> None:
        """All 9 lifecycle finding-kind constants must be in types.py."""
        import medre.core.diagnostics.convergence.types as types_mod

        for name in _LIFECYCLE_KINDS:
            assert hasattr(types_mod, name), f"types.py must define {name!r}"
            value = getattr(types_mod, name)
            assert (
                isinstance(value, str) and value
            ), f"{name!r} must be a non-empty string constant"

    def test_kind_constants_have_expected_values(self) -> None:
        """Each lifecycle kind constant must be a lowercase snake_case string."""
        from medre.core.diagnostics.convergence import types as types_mod

        for name in _LIFECYCLE_KINDS:
            value = getattr(types_mod, name)
            assert value == value.lower(), f"{name!r}={value!r} must be lowercase"
            assert " " not in value, f"{name!r}={value!r} must not contain spaces"

    def test_kind_constants_not_re_exported_from_package_root(self) -> None:
        """Lifecycle kind constants must NOT be on the convergence package."""
        import medre.core.diagnostics.convergence as pkg

        for name in _LIFECYCLE_KINDS:
            assert not hasattr(
                pkg, name
            ), f"convergence package must not re-export {name!r}"


# ---------------------------------------------------------------------------
# Convergence __init__.py still docs-only
# ---------------------------------------------------------------------------


class TestInitRemainsDocsOnly:
    """Convergence __init__.py must not gain imports or __all__."""

    def test_init_no_import_statements(self) -> None:
        source = _CONVERGENCE_INIT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                pytest.fail(
                    f"__init__.py must not contain import statements; "
                    f"found import at line {node.lineno}"
                )

    def test_init_no_dunder_all(self) -> None:
        source = _CONVERGENCE_INIT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        pytest.fail("__init__.py must not define __all__")


# ---------------------------------------------------------------------------
# Purity — no storage / runtime / adapter / evidence imports
# ---------------------------------------------------------------------------


class TestModulePurity:
    """lifecycle_convergence.py must be pure — no infrastructure imports."""

    def test_no_forbidden_imports(self) -> None:
        source = _LIFECYCLE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for part in _FORBIDDEN_IMPORT_PARTS:
                    assert part not in node.module, (
                        f"lifecycle_convergence.py must not import from "
                        f"{part!r}; found 'from {node.module}' at line {node.lineno}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for part in _FORBIDDEN_IMPORT_PARTS:
                        assert part not in alias.name, (
                            f"lifecycle_convergence.py must not import "
                            f"{part!r}; found 'import {alias.name}' at line {node.lineno}"
                        )

    def test_no_forbidden_mutation_calls(self) -> None:
        """Module source must not call write/mutation APIs at module level or in functions."""
        source = _LIFECYCLE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Handle direct calls like write(...), save(...)
                if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALL_NAMES:
                    pytest.fail(
                        f"lifecycle_convergence.py must not call {func.id}() "
                        f"at line {node.lineno}"
                    )
                # Handle attribute calls like db.write(...)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in _FORBIDDEN_CALL_NAMES
                ):
                    pytest.fail(
                        f"lifecycle_convergence.py must not call .{func.attr}() "
                        f"at line {node.lineno}"
                    )

    def test_imports_only_stdlib_and_package_internals(self) -> None:
        """All imports in lifecycle_convergence.py must be stdlib or relative."""
        source = _LIFECYCLE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Relative imports (level > 0) are always ok
                if node.level and node.level > 0:
                    continue
                # Absolute imports must be stdlib
                assert node.module.startswith(
                    (
                        "datetime",
                        "typing",
                        "collections",
                        "dataclasses",
                        "enum",
                        "functools",
                        "itertools",
                        "json",
                        "re",
                        "math",
                        "pathlib",
                        "__future__",
                    )
                ), (
                    f"lifecycle_convergence.py must import only stdlib or "
                    f"relative package modules; found 'from {node.module}' "
                    f"at line {node.lineno}"
                )


# ---------------------------------------------------------------------------
# Function signature — accepts Iterable (generator) inputs
# ---------------------------------------------------------------------------


class TestFunctionSignature:
    """build_lifecycle_convergence_findings must accept generator inputs."""

    def test_outbox_items_parameter_is_iterable(self) -> None:
        from medre.core.diagnostics.convergence.lifecycle_convergence import (
            build_lifecycle_convergence_findings,
        )

        sig = inspect.signature(build_lifecycle_convergence_findings)
        sig.parameters["outbox_items"]

        # The annotation should be Iterable or a compatible type
        # We just verify the function works with a generator
        def gen():
            yield {
                "outbox_id": "ob-1",
                "status": "pending",
                "delivery_plan_id": "p-1",
                "target_adapter": "a",
                "target_channel": None,
                "attempt_number": 1,
                "event_id": "e-1",
            }

        result = build_lifecycle_convergence_findings(outbox_items=gen())
        assert isinstance(result, list)

    def test_receipts_parameter_is_iterable(self) -> None:
        from medre.core.diagnostics.convergence.lifecycle_convergence import (
            build_lifecycle_convergence_findings,
        )

        def gen():
            yield {
                "receipt_id": "r-1",
                "status": "sent",
                "delivery_plan_id": "p-1",
                "target_adapter": "a",
                "target_channel": None,
                "attempt_number": 1,
                "sequence": 1,
                "failure_kind": "",
                "event_id": "e-1",
                "created_at": "2026-05-31T12:00:00+00:00",
            }

        result = build_lifecycle_convergence_findings(receipts=gen())
        assert isinstance(result, list)

    def test_both_generators_simultaneously(self) -> None:
        """Verify both parameters accept generators at the same time."""
        from medre.core.diagnostics.convergence.lifecycle_convergence import (
            build_lifecycle_convergence_findings,
        )

        def outbox_gen():
            yield {
                "outbox_id": "ob-1",
                "status": "retry_wait",
                "delivery_plan_id": "p-1",
                "target_adapter": "a",
                "target_channel": None,
                "attempt_number": 1,
                "event_id": "e-1",
            }

        def receipt_gen():
            yield {
                "receipt_id": "r-1",
                "status": "failed",
                "delivery_plan_id": "p-1",
                "target_adapter": "a",
                "target_channel": None,
                "attempt_number": 1,
                "sequence": 1,
                "failure_kind": "adapter_transient",
                "event_id": "e-1",
                "created_at": "2026-05-31T12:00:00+00:00",
            }

        result = build_lifecycle_convergence_findings(
            outbox_items=outbox_gen(), receipts=receipt_gen()
        )
        assert isinstance(result, list)
