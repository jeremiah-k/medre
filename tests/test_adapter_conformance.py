"""Adapter conformance tests.

Verifies that every adapter (real and fake) conforms to the core
adapter contract shape using concrete imports only — no package-root
facade imports.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
from pathlib import Path

import pytest

# Concrete adapter imports — no package-root facades.
from medre.adapters.fake_lxmf import FakeLxmfAdapter
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.adapters.fake_transport import FakeTransportAdapter
from medre.core.contracts.adapter import AdapterContract

# Fake adapters that can be instantiated without SDKs.
_FAKE_ADAPTERS: list[type[AdapterContract]] = [
    FakeLxmfAdapter,
    FakeMatrixAdapter,
    FakeMeshCoreAdapter,
    FakeMeshtasticAdapter,
    FakePresentationAdapter,
    FakeTransportAdapter,
]


class TestFakeAdapterConformance:
    """Fake adapters must conform to the core contract."""

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_class_is_contract(self, cls: type) -> None:
        """Adapter class should be a subclass of AdapterContract."""
        assert issubclass(
            cls, AdapterContract
        ), f"{cls.__name__} is not a subclass of AdapterContract"

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_has_expected_lifecycle_methods(self, cls: type) -> None:
        """Adapter class should expose async lifecycle methods."""
        assert hasattr(cls, "start"), f"{cls.__name__} lacks start"
        assert hasattr(cls, "stop"), f"{cls.__name__} lacks stop"
        assert hasattr(cls, "health_check"), f"{cls.__name__} lacks health_check"
        assert hasattr(cls, "deliver"), f"{cls.__name__} lacks deliver"

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_can_be_instantiated_with_minimal_args(self, cls: type) -> None:
        """Fake adapter should be constructable with minimal args."""
        instance = cls(adapter_id="test")
        assert instance is not None
        assert instance.adapter_id == "test"

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_has_platform(self, cls: type) -> None:
        """Adapter class should expose a platform attribute."""
        instance = cls(adapter_id="test")
        platform = getattr(instance, "platform", None)
        assert platform is not None, f"{cls.__name__} has no platform"
        assert isinstance(platform, str), f"{cls.__name__} platform not str"

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_lifecycle_methods_are_async(self, cls: type) -> None:
        """start, stop, health_check, deliver must be coroutine functions."""
        for method_name in ("start", "stop", "health_check", "deliver"):
            method = getattr(cls, method_name, None)
            assert method is not None, f"{cls.__name__} missing {method_name}"
            assert inspect.iscoroutinefunction(
                method
            ), f"{cls.__name__}.{method_name} must be async"


class TestRealAdapterContractImports:
    """Real adapter classes must be importable from concrete paths.

    Does NOT instantiate — only verifies the imports resolve.
    """

    @pytest.mark.skipif(
        not importlib.util.find_spec("nio"),
        reason="nio not installed",
    )
    def test_matrix_adapter_importable(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        assert issubclass(MatrixAdapter, AdapterContract)

    @pytest.mark.skipif(
        not importlib.util.find_spec("meshtastic"),
        reason="meshtastic not installed",
    )
    def test_meshtastic_adapter_importable(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        assert issubclass(MeshtasticAdapter, AdapterContract)

    @pytest.mark.skipif(
        not importlib.util.find_spec("meshcore"),
        reason="meshcore not installed",
    )
    def test_meshcore_adapter_importable(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        assert issubclass(MeshCoreAdapter, AdapterContract)

    @pytest.mark.skipif(
        not (importlib.util.find_spec("RNS") and importlib.util.find_spec("LXMF")),
        reason="RNS and/or LXMF not installed",
    )
    def test_lxmf_adapter_importable(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        assert issubclass(LxmfAdapter, AdapterContract)


class TestNoPackageRootAdapterImports:
    """Conformance tests must not import from package-root facades.

    Also verifies that adapter module imports (e.g.
    ``medre.adapters.matrix.adapter``) succeed even without optional
    SDKs installed, because SDK guards live behind compat/session
    boundaries — not at the adapter-module level.
    """

    # Adapter modules whose import must work without optional SDKs.
    _ADAPTER_MODULES: list[str] = [
        "medre.adapters.matrix.adapter",
        "medre.adapters.meshtastic.adapter",
        "medre.adapters.meshcore.adapter",
        "medre.adapters.lxmf.adapter",
    ]

    def test_not_importing_from_adapters_root(self) -> None:
        """Verify this test file doesn't use package-root facade imports.

        Uses AST parsing to avoid false positives from comments or strings.
        Imports inside TYPE_CHECKING blocks are allowed since they are
        type-checking only.  Concrete submodule imports (e.g.
        ``from medre.adapters.matrix.adapter import MatrixAdapter``) are
        fine; only bare package-root facades like
        ``from medre.adapters import MatrixAdapter`` are forbidden.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Build parent map for scope-aware TYPE_CHECKING detection
        parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parent[child] = node

        def _inside_type_checking(n: ast.AST) -> bool:
            cur = n
            while cur in parent:
                cur = parent[cur]
                if isinstance(cur, ast.If):
                    test = cur.test
                    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                        return True
            return False

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            # Only look at imports from medre.adapters
            if not module.startswith("medre.adapters"):
                continue
            # Concrete submodule imports are fine (e.g. medre.adapters.matrix.adapter)
            parts = module.split(".")
            # medre.adapters has 2 parts; anything deeper is a concrete submodule
            if len(parts) > 2:
                continue
            # Skip if this import is inside a TYPE_CHECKING guard
            if _inside_type_checking(node):
                continue
            # This is a package-root facade import
            names = ", ".join(alias.name for alias in (node.names or []))
            pytest.fail(
                f"Conformance test uses package-root import: "
                f"from {module} import {names}"
            )

    @pytest.mark.parametrize("module_name", _ADAPTER_MODULES)
    def test_adapter_module_imports_without_sdk(self, module_name: str) -> None:
        """Importing the adapter module itself must not fail without SDKs.

        SDK guards are behind compat/session boundaries, so
        ``import medre.adapters.matrix.adapter`` should succeed even when
        ``nio`` is not installed.
        """
        import importlib

        mod = importlib.import_module(module_name)
        assert mod is not None, f"Failed to import {module_name}"
