"""Import boundary enforcement tests.

Ensure core, config, and source modules follow canonical import rules
and that old/noncanonical modules are not referenced (sections G–L).

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers.import_scanner import scan_dir_for_prefixes

# ===================================================================
# G) Core → adapters import boundary
# ===================================================================


class TestCoreDoesNotImportAdapters:
    """Core modules must not have runtime imports from medre.adapters.

    Adapter contract types live in medre.core.contracts.adapter.
    Core must not depend on adapters or config.
    """

    def test_no_runtime_core_to_adapters_imports(self) -> None:
        """Scan all core .py files for medre.adapters imports."""
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.adapters") or stripped.startswith(
                    "import medre.adapters"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Core modules must not import from medre.adapters:\n" + "\n".join(violations)


# ===================================================================
# H) Config → adapters import boundary
# ===================================================================


class TestConfigDoesNotImportAdapters:
    """Config modules must not import concrete adapter packages.

    Adapter config models live in medre.config.adapters.*, not in
    medre.adapters.*.config.  The config/adapters/ subpackage must
    also not import from medre.adapters (credential helpers are
    config-owned).
    """

    def test_no_config_to_adapters_imports(self) -> None:
        """Scan ALL config .py files for medre.adapters imports."""
        repo_root = Path(__file__).resolve().parents[1]
        config_dir = repo_root / "src" / "medre" / "config"
        assert config_dir.exists(), f"config directory not found: {config_dir}"

        violations: list[str] = []
        for py_file in sorted(config_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.adapters") or stripped.startswith(
                    "import medre.adapters"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Config modules must not import from medre.adapters:\n" + "\n".join(
            violations
        )


# ===================================================================
# I) Core does not import config
# ===================================================================


class TestCoreDoesNotImportConfig:
    """Core modules must not import from medre.config.

    Core is the innermost layer and must not depend on config.
    """

    def test_no_core_to_config_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.config") or stripped.startswith(
                    "import medre.config"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Core modules must not import from medre.config:\n" + "\n".join(violations)


# ===================================================================
# J) No old/noncanonical imports remain in src or tests
# ===================================================================


class TestNoOldImports:
    """No source or test file may import from old/noncanonical modules.

    Enforces that the following old modules are not referenced:
    - medre.adapters.base
    - medre.core.ports
    - medre.core.adapter_base
    - medre.adapters.*.config (config dataclasses live in medre.config.adapters.*)
    - ConfigError classes from medre.adapters.*.errors
    """

    # Forbidden import prefixes — these old modules must not be imported.
    _FORBIDDEN_PREFIXES = (
        "from medre.adapters.base",
        "import medre.adapters.base",
        "from medre.core.ports",
        "import medre.core.ports",
        "from medre.core.adapter_base",
        "import medre.core.adapter_base",
        "from medre.adapters.matrix.config",
        "from medre.adapters.meshtastic.config",
        "from medre.adapters.meshcore.config",
        "from medre.adapters.lxmf.config",
        "import medre.adapters.matrix.config",
        "import medre.adapters.meshtastic.config",
        "import medre.adapters.meshcore.config",
        "import medre.adapters.lxmf.config",
        "from medre.adapters.matrix.errors import MatrixConfigError",
        "from medre.adapters.meshtastic.errors import MeshtasticConfigError",
        "from medre.adapters.meshcore.errors import MeshCoreConfigError",
        "from medre.adapters.lxmf.errors import LxmfConfigError",
    )

    def test_no_old_imports_in_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_dir = repo_root / "src"
        assert src_dir.exists()

        violations = scan_dir_for_prefixes(src_dir, self._FORBIDDEN_PREFIXES)
        assert (
            violations == []
        ), "Old/noncanonical imports found in src/:\n" + "\n".join(violations)

    def test_no_old_imports_in_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tests_dir = repo_root / "tests"
        assert tests_dir.exists()

        violations = scan_dir_for_prefixes(tests_dir, self._FORBIDDEN_PREFIXES)
        assert (
            violations == []
        ), "Old/noncanonical imports found in tests/:\n" + "\n".join(violations)


# ===================================================================
# K) No BaseAdapter references in source or tests
# ===================================================================


class TestNoOldAdapterBaseReferences:
    """No source or test file should reference the old adapter base class name.

    The old name has been renamed to AdapterContract.  All source and
    test code should use AdapterContract instead.
    """

    # Use string concat to avoid the literal appearing in this test file.
    _OLD_NAME = "Base" + "Adapter"

    def test_no_baseadapter_in_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_dir = repo_root / "src"
        assert src_dir.exists()

        violations: list[str] = []
        old_name = self._OLD_NAME
        for py_file in sorted(src_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if old_name in stripped:
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert violations == [], f"{old_name} references found in src/:\n" + "\n".join(
            violations
        )

    def test_no_baseadapter_in_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tests_dir = repo_root / "tests"
        assert tests_dir.exists()

        violations: list[str] = []
        old_name = self._OLD_NAME
        for py_file in sorted(tests_dir.rglob("*.py")):
            # Test files that reference the old name in their scan
            # logic — exclude them from the scan.
            if py_file.name in (
                "test_boundary_imports.py",
                "test_boundary_api.py",
            ):
                continue
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if old_name in stripped:
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert (
            violations == []
        ), f"{old_name} references found in tests/:\n" + "\n".join(violations)


# ===================================================================
# L) Old/noncanonical module files do not exist
# ===================================================================


class TestOldModulesRemoved:
    """Old/noncanonical module files must not exist on disk."""

    _EXPECTED_ABSENT = [
        "src/medre/core/ports.py",
        "src/medre/core/adapter_base.py",
        "src/medre/adapters/base.py",
        "src/medre/adapters/matrix/config.py",
        "src/medre/adapters/meshtastic/config.py",
        "src/medre/adapters/meshcore/config.py",
        "src/medre/adapters/lxmf/config.py",
    ]

    def test_old_modules_removed(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        remaining = [p for p in self._EXPECTED_ABSENT if (repo_root / p).exists()]
        assert remaining == [], "Old/noncanonical modules still exist:\n" + "\n".join(
            remaining
        )
