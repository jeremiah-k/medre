"""Architecture boundary tests: forbidden imports and dependency direction.

Uses AST analysis to verify that source files don't import forbidden
modules at runtime scope.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.ast_imports import (
    import_matches,
    parse_python,
    runtime_scope_imports,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"

# Forbidden prefixes for core modules
_CORE_FORBIDDEN: tuple[str, ...] = (
    "medre.adapters",
    "medre.runtime.builder",
    "medre.cli",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
)

# Forbidden for route_engine
_ROUTE_ENGINE_FORBIDDEN: tuple[str, ...] = (
    "medre.adapters",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
    "medre.runtime.builder",
)

# Allowed adapter-specific prefixes for config/model.py
_CONFIG_ALLOWED_ADAPTER: tuple[str, ...] = (
    "medre.config.adapters.matrix",
    "medre.config.adapters.meshtastic",
    "medre.config.adapters.meshcore",
    "medre.config.adapters.lxmf",
)

# Forbidden for config/model.py
_CONFIG_FORBIDDEN: tuple[str, ...] = (
    "medre.adapters",
    "medre.runtime.builder",
    "medre.runtime.route_engine",
    "medre.core.engine",
    "nio",
    "meshtastic",
    "aiohttp",
    "serial",
    "serial_asyncio",
)


def _find_py_files(directory: Path) -> list[Path]:
    """Return sorted list of .py files in *directory*."""
    return sorted(directory.rglob("*.py"))


def _check_forbidden(
    py_file: Path,
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    """Check *py_file* for forbidden runtime-scope imports.

    Returns violation descriptions.
    """
    tree = parse_python(py_file)
    imports = runtime_scope_imports(tree, file_path=str(py_file))
    violations: list[str] = []
    for imp in imports:
        if import_matches(imp.module, forbidden_prefixes):
            rel = py_file.relative_to(_REPO)
            violations.append(f"{rel}:{imp.lineno}: imports {imp.module}")
    return violations


class TestCoreBoundary:
    """Core modules must not import runtime, adapters, SDKs, or CLI."""

    @pytest.fixture(scope="class")
    def core_py_files(self) -> list[Path]:
        core_dir = _SRC / "core"
        return _find_py_files(core_dir)

    def test_core_no_forbidden_imports(self, core_py_files: list[Path]) -> None:
        all_violations: list[str] = []
        for py_file in core_py_files:
            violations = _check_forbidden(py_file, _CORE_FORBIDDEN)
            all_violations.extend(violations)
        assert not all_violations, (
            "Core modules have forbidden runtime-scope imports:\n" +
            "\n".join(all_violations)
        )


class TestRouteEngineBoundary:
    """Route engine must not import adapters, SDKs, or builder."""

    def test_route_engine_no_forbidden_imports(self) -> None:
        py_file = _SRC / "runtime" / "route_engine.py"
        assert py_file.exists(), f"File not found: {py_file}"
        violations = _check_forbidden(py_file, _ROUTE_ENGINE_FORBIDDEN)
        assert not violations, (
            "route_engine.py has forbidden imports:\n" +
            "\n".join(violations)
        )


class TestConfigModelBoundary:
    """config/model.py may only import adapter config dataclasses."""

    def test_config_model_allows_adapter_configs(self) -> None:
        py_file = _SRC / "config" / "model.py"
        assert py_file.exists(), f"File not found: {py_file}"
        tree = parse_python(py_file)
        imports = runtime_scope_imports(tree, file_path=str(py_file))
        violations: list[str] = []
        rel = py_file.relative_to(_REPO)
        for imp in imports:
            # Check forbidden first
            if import_matches(imp.module, _CONFIG_FORBIDDEN):
                violations.append(f"{rel}:{imp.lineno}: forbidden import {imp.module}")
            # Check that adapter imports are only allowed adapter configs
            if imp.module.startswith("medre.config.adapters") and not import_matches(
                imp.module, _CONFIG_ALLOWED_ADAPTER
            ):
                violations.append(
                    f"{rel}:{imp.lineno}: unregistered adapter config {imp.module}"
                )
        assert not violations, (
            "config/model.py has import violations:\n" +
            "\n".join(violations)
        )
