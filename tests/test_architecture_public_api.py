"""Architecture boundary tests: no public API commitment yet.

Ensures that importing top-level packages does not pull in heavy
implementation modules, and that public facade paths are not required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


class TestTopLevelImportLightweight:
    """Importing 'medre' must not pull in heavy modules."""

    _FORBIDDEN_AFTER_IMPORT: tuple[str, ...] = (
        "medre.runtime.builder",
        "medre.core.engine.pipeline",
        "medre.core.storage",
        "medre.adapters.matrix.adapter",
        "medre.adapters.meshtastic.adapter",
        "nio",
        "meshtastic",
    )

    def test_import_medre_is_lightweight(self) -> None:
        """import medre must not pull in heavy modules.

        Runs in a subprocess to get a cold interpreter — avoids false
        positives from modules already cached in the test process.
        """
        import json
        import subprocess
        import textwrap

        code = textwrap.dedent(f"""
            import json, sys
            forbidden = {self._FORBIDDEN_AFTER_IMPORT!r}
            before = set(sys.modules)
            import medre  # noqa: F401
            after = set(sys.modules)
            loaded = sorted((after - before) & set(forbidden))
            print(json.dumps(loaded))
            """)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
        loaded = json.loads(proc.stdout.strip() or "[]")
        assert not loaded, f"importing 'medre' pulled in heavy module(s): {loaded}"

    def test_medre_has_no_substantive_all(self) -> None:
        import medre  # noqa: F811

        # __all__ should not contain runtime builders, adapters, pipeline
        if hasattr(medre, "__all__"):
            api_symbols = set(
                medre.__all__
            )  # pyright: ignore[reportAttributeAccessIssue]
            forbidden = {
                "RuntimeBuilder",
                "MedreApp",
                "PipelineRunner",
                "PipelineConfig",
                "MatrixAdapter",
                "MeshtasticAdapter",
                "sanitize_error",
                "sanitize_for_log",
            }
            found = api_symbols & forbidden
            assert not found, f"medre.__all__ contains public API symbols: {found}"


class TestObservabilityFacadeRemoved:
    """medre.observability must not exist as a package."""

    def test_import_medre_observability_raises_module_not_found(self) -> None:
        """importing medre.observability must raise ModuleNotFoundError."""
        with pytest.raises(ModuleNotFoundError):
            import medre.observability  # type: ignore[import-not-found,unused-import]  # noqa: F401

    def test_observability_sanitization_module_does_not_exist(self) -> None:
        """The medre.observability.sanitization re-export module must not exist."""
        from pathlib import Path

        # The module lives at medre.core.observability.sanitization now
        # Check the old facade path doesn't exist
        src_dir = Path(__file__).resolve().parents[1] / "src" / "medre"
        old_path = src_dir / "observability" / "sanitization.py"
        assert not old_path.exists(), "medre.observability.sanitization.py still exists"

    def test_from_medre_observability_import_fails(self) -> None:
        """medre.observability should not be importable as a facade."""
        import medre.core.observability.sanitization  # noqa: F401 — canonical path works

        with pytest.raises(ImportError):
            from medre.observability import (  # type: ignore[import-not-found]  # noqa: F401
                sanitize_error,
            )


class TestConfigFacadeRemoved:
    """Config packages must not expose convenience re-exports."""

    def test_config_has_no_all(self) -> None:
        import importlib

        mod = importlib.import_module("medre.config")
        if hasattr(mod, "__all__"):
            assert not mod.__all__, f"medre.config exposes __all__: {mod.__all__}"

    def test_config_adapters_has_no_all(self) -> None:
        import importlib

        mod = importlib.import_module("medre.config.adapters")
        if hasattr(mod, "__all__"):
            assert (
                not mod.__all__
            ), f"medre.config.adapters exposes __all__: {mod.__all__}"


class TestRunSessionFacadeRemoved:
    """medre.runtime.run_session must not expose convenience re-exports."""

    def test_run_session_has_no_bridge_session(self) -> None:
        import importlib

        mod = importlib.import_module("medre.runtime.run_session")
        assert not hasattr(
            mod, "run_bridge_session"
        ), "medre.runtime.run_session should not re-export run_bridge_session"

    def test_run_session_has_no_scenario_category(self) -> None:
        import importlib

        mod = importlib.import_module("medre.runtime.run_session")
        assert not hasattr(
            mod, "scenario_category"
        ), "medre.runtime.run_session should not re-export scenario_category"


class TestEvidenceFacadeRemoved:
    """medre.runtime.evidence must not expose convenience re-exports."""

    def test_evidence_has_no_collect_bundle(self) -> None:
        import importlib

        mod = importlib.import_module("medre.runtime.evidence")
        assert not hasattr(
            mod, "collect_evidence_bundle"
        ), "medre.runtime.evidence should not re-export collect_evidence_bundle"


class TestPackageRootsNoFormerSymbols:
    """Package roots must not expose former convenience symbols."""

    def _mod(self, name: str):
        import importlib

        return importlib.import_module(name)

    def test_adapters_no_former_symbols(self) -> None:
        mod = self._mod("medre.adapters")
        forbidden = [
            "FakeMatrixAdapter",
            "FakeMeshtasticAdapter",
            "FakeMeshCoreAdapter",
            "FakeLxmfAdapter",
            "FakePresentationAdapter",
            "FakeTransportAdapter",
            "FaultyPresentationAdapter",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.adapters exposes {sym}"

    def test_adapters_matrix_no_former_symbols(self) -> None:
        mod = self._mod("medre.adapters.matrix")
        forbidden = [
            "MatrixAdapter",
            "MatrixCodec",
            "MatrixRenderer",
            "MatrixSession",
            "MatrixConfig",
            "MatrixConnectionError",
            "MatrixSendError",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.adapters.matrix exposes {sym}"

    def test_adapters_meshtastic_no_former_symbols(self) -> None:
        mod = self._mod("medre.adapters.meshtastic")
        forbidden = [
            "MeshtasticAdapter",
            "MeshtasticCodec",
            "MeshtasticRenderer",
            "MeshtasticSession",
            "MeshtasticConfig",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.adapters.meshtastic exposes {sym}"

    def test_adapters_meshcore_no_former_symbols(self) -> None:
        mod = self._mod("medre.adapters.meshcore")
        forbidden = [
            "MeshCoreAdapter",
            "MeshCoreCodec",
            "MeshCoreRenderer",
            "MeshCoreSession",
            "MeshCoreConfig",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.adapters.meshcore exposes {sym}"

    def test_adapters_lxmf_no_former_symbols(self) -> None:
        mod = self._mod("medre.adapters.lxmf")
        forbidden = [
            "LxmfAdapter",
            "LxmfCodec",
            "LxmfRenderer",
            "LxmfSession",
            "LxmfConfig",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.adapters.lxmf exposes {sym}"

    def test_config_no_former_symbols(self) -> None:
        mod = self._mod("medre.config")
        forbidden = [
            "RuntimeConfig",
            "load_config",
            "MedrePaths",
            "RouteConfig",
            "MatrixConfig",
        ]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.config exposes {sym}"

    def test_config_adapters_no_former_symbols(self) -> None:
        mod = self._mod("medre.config.adapters")
        forbidden = ["MatrixConfig", "MeshtasticConfig", "MeshCoreConfig", "LxmfConfig"]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.config.adapters exposes {sym}"

    def test_runtime_no_former_symbols(self) -> None:
        mod = self._mod("medre.runtime")
        forbidden = ["RuntimeBuilder", "MedreApp", "RuntimeStartupError", "RouteConfig"]
        for sym in forbidden:
            assert not hasattr(mod, sym), f"medre.runtime exposes {sym}"


class TestConcretePathsWork:
    """Canonical concrete import paths must work."""

    def test_import_adapter_matrix(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        assert MatrixAdapter is not None

    def test_import_codec_matrix(self) -> None:
        from medre.adapters.matrix.codec import MatrixCodec

        assert MatrixCodec is not None

    def test_import_config_matrix(self) -> None:
        from medre.config.adapters.matrix import MatrixConfig

        assert MatrixConfig is not None

    def test_import_config_model(self) -> None:
        from medre.config.model import RuntimeConfig

        assert RuntimeConfig is not None

    def test_import_loader(self) -> None:
        from medre.config.loader import load_config

        assert load_config is not None

    def test_import_runtime_builder(self) -> None:
        from medre.runtime.builder import RuntimeBuilder

        assert RuntimeBuilder is not None

    def test_import_timeline(self) -> None:
        import medre.runtime.timeline as timeline

        assert timeline is not None

    def test_import_sanitization(self) -> None:
        from medre.core.observability.sanitization import (
            sanitize_error,
            sanitize_for_log,
        )

        assert sanitize_error is not None
        assert sanitize_for_log is not None


class TestPackageRootsSystematic:
    """Package roots outside medre.core.* must be lightweight markers only.

    Walk selected __init__.py files and reject:
    - __all__ (must be absent)
    - __getattr__ (must be absent)
    - from .x import Symbol (re-exports from submodules)
    - from medre.x import Symbol (cross-package re-exports)
    - assignment of former API names
    """

    # Package roots to check (relative to src/medre/).
    # These are outside medre.core.* and should have no facade surface.
    _PACKAGE_ROOTS = [
        "medre/__init__.py",
        "medre/adapters/__init__.py",
        "medre/adapters/matrix/__init__.py",
        "medre/adapters/meshtastic/__init__.py",
        "medre/adapters/meshcore/__init__.py",
        "medre/adapters/lxmf/__init__.py",
        "medre/config/__init__.py",
        "medre/config/adapters/__init__.py",
        "medre/core/storage/__init__.py",
        "medre/core/storage/sqlite/__init__.py",
        "medre/runtime/__init__.py",
        "medre/runtime/evidence/__init__.py",
        "medre/runtime/run_session/__init__.py",
    ]

    def _read_py_file(self, rel_path: str) -> tuple[Path, str]:
        src_dir = Path(__file__).resolve().parents[1] / "src"
        py_file = src_dir / rel_path
        assert py_file.exists(), f"File not found: {py_file}"
        return py_file, py_file.read_text(encoding="utf-8")

    def test_all_roots_have_no_all(self) -> None:
        """__all__ must not appear in package roots outside medre.core.*."""
        import ast

        for rel in self._PACKAGE_ROOTS:
            _file, source = self._read_py_file(rel)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__all__":
                            pytest.fail(
                                f"{rel}: defines __all__ — package roots outside "
                                f"medre.core.* must not declare __all__"
                            )
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.target, ast.Name
                ):
                    if node.target.id == "__all__":
                        pytest.fail(
                            f"{rel}: defines __all__ — package roots outside "
                            f"medre.core.* must not declare __all__"
                        )

    def test_all_roots_have_no_getattr(self) -> None:
        """__getattr__ must be absent (AST-based, avoids docstring false positives)."""
        import ast

        for rel in self._PACKAGE_ROOTS:
            _file, source = self._read_py_file(rel)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name == "__getattr__":
                        pytest.fail(f"{rel}: defines __getattr__")
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__getattr__":
                            pytest.fail(f"{rel}: assigns __getattr__")
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.target, ast.Name
                ):
                    if node.target.id == "__getattr__":
                        pytest.fail(f"{rel}: assigns __getattr__")

    def test_all_roots_have_no_submodule_re_exports(self) -> None:
        """No 'from .x import Symbol' re-exports from submodules."""
        import ast

        for rel in self._PACKAGE_ROOTS:
            _file, source = self._read_py_file(rel)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    # Relative imports: from .x import Y
                    if node.level and node.level > 0:
                        names = [a.name for a in node.names]
                        pytest.fail(f"{rel}: re-exports from submodule: {names}")
                    # Absolute imports from medre.*
                    if node.module and node.module.startswith("medre."):
                        names = [a.name for a in node.names]
                        pytest.fail(f"{rel}: re-exports from {node.module}: {names}")
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                    for n in names:
                        if n.startswith("medre.") and n.count(".") >= 2:
                            pytest.fail(f"{rel}: re-exports medre module: {n}")

    def test_all_roots_have_no_former_name_assignments(self) -> None:
        """No assignment of former API names (e.g., RuntimeConfig = ...)."""
        import ast

        former_names = {
            "RuntimeConfig",
            "load_config",
            "RouteConfig",
            "MedrePaths",
            "RuntimeBuilder",
            "MedreApp",
            "RuntimeStartupError",
            "MatrixAdapter",
            "MatrixCodec",
            "MatrixRenderer",
            "MatrixSession",
            "MatrixConfig",
            "MatrixConfigError",
            "MeshtasticAdapter",
            "MeshtasticCodec",
            "MeshtasticRenderer",
            "MeshtasticSession",
            "MeshtasticConfig",
            "MeshCoreAdapter",
            "MeshCoreCodec",
            "MeshCoreRenderer",
            "MeshCoreSession",
            "MeshCoreConfig",
            "LxmfAdapter",
            "LxmfCodec",
            "LxmfRenderer",
            "LxmfSession",
            "LxmfConfig",
            "FakeMatrixAdapter",
            "FakeMeshtasticAdapter",
            "FakeMeshCoreAdapter",
            "FakeLxmfAdapter",
            "FakePresentationAdapter",
            "FakeTransportAdapter",
            "collect_evidence_bundle",
            "run_bridge_session",
            # Storage facade symbols — must not be re-exported from package roots
            "SQLiteStorage",
            "STALE_QUEUED_GRACE_SECONDS",
            "EventFilter",
            "DeliveryOutboxItem",
            "StorageBackend",
            "StorageGuarantees",
            "StorageError",
            "DuplicateEventError",
            "EventNotFoundError",
            "StorageInitializationError",
            "SchemaValidationError",
        }
        for rel in self._PACKAGE_ROOTS:
            _file, source = self._read_py_file(rel)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if target.id in former_names:
                                pytest.fail(
                                    f"{rel}: assigns former API name '{target.id}'"
                                )
                elif isinstance(node, ast.AnnAssign) and isinstance(
                    node.target, ast.Name
                ):
                    if node.target.id in former_names:
                        pytest.fail(
                            f"{rel}: assigns former API name '{node.target.id}'"
                        )

    def test_observability_does_not_exist(self) -> None:
        """medre.observability must not exist as a package."""
        obs_dir = (
            Path(__file__).resolve().parents[1] / "src" / "medre" / "observability"
        )
        assert not obs_dir.exists(), "medre/observability/ directory still exists"

    def test_storage_init_has_no_symbols(self) -> None:
        """medre.core.storage must not expose any storage symbols."""
        import medre.core.storage as storage_pkg

        forbidden = [
            "SQLiteStorage",
            "STALE_QUEUED_GRACE_SECONDS",
            "EventFilter",
            "DeliveryOutboxItem",
            "StorageBackend",
            "StorageGuarantees",
            "StorageError",
            "DuplicateEventError",
            "EventNotFoundError",
            "StorageInitializationError",
            "SchemaValidationError",
        ]
        for sym in forbidden:
            assert not hasattr(storage_pkg, sym), (
                f"medre.core.storage should not expose {sym}; "
                f"import from canonical module instead"
            )

    def test_storage_sqlite_init_has_no_symbols(self) -> None:
        """medre.core.storage.sqlite must not expose any storage symbols."""
        import medre.core.storage.sqlite as sqlite_pkg

        forbidden = [
            "SQLiteStorage",
            "STALE_QUEUED_GRACE_SECONDS",
        ]
        for sym in forbidden:
            assert not hasattr(sqlite_pkg, sym), (
                f"medre.core.storage.sqlite should not expose {sym}; "
                f"import from canonical module instead"
            )
