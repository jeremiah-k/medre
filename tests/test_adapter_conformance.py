"""Adapter conformance tests.

Verifies that every adapter (real and fake) conforms to the core
adapter contract shape using concrete imports only — no package-root
facade imports.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

# Concrete adapter imports — no package-root facades.
from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.contracts.adapter import AdapterContract
from medre.runtime.architecture_ast import runtime_scope_imports

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


# Adapter classes that must be importable without SDKs.
# SDK guards live behind compat/session boundaries, not at the adapter-module level.
_ADAPTER_CLASSES: list[tuple[str, str]] = [
    ("medre.adapters.matrix.adapter", "MatrixAdapter"),
    ("medre.adapters.meshtastic.adapter", "MeshtasticAdapter"),
    ("medre.adapters.meshcore.adapter", "MeshCoreAdapter"),
    ("medre.adapters.lxmf.adapter", "LxmfAdapter"),
]


class TestRealAdapterContractImports:
    """Real adapter classes must be importable and conform to AdapterContract.

    Does NOT instantiate — only verifies class presence and subclassing.
    Adapter modules import SDK-free because SDK guards live behind
    compat/session boundaries.
    """

    @pytest.mark.parametrize("module_name, class_name", _ADAPTER_CLASSES)
    def test_adapter_class_is_contract(self, module_name: str, class_name: str) -> None:
        import importlib

        mod = importlib.import_module(module_name)
        cls = getattr(mod, class_name)
        assert issubclass(
            cls, AdapterContract
        ), f"{class_name} is not a subclass of AdapterContract"


_FORBIDDEN_ADAPTER_ROOTS = frozenset(
    {
        "medre.adapters",
        "medre.adapters.matrix",
        "medre.adapters.meshtastic",
        "medre.adapters.meshcore",
        "medre.adapters.lxmf",
    }
)
"""Adapter package roots that must not be used as import sources."""


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

        Uses runtime_scope_imports() to inspect only imports that execute
        at runtime — imports inside ``if TYPE_CHECKING:`` bodies are
        correctly excluded while ``else:`` branch imports are flagged.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Use canonical helper: only runtime-scope imports are returned,
        # so TYPE_CHECKING body imports are automatically excluded.
        violations: list[str] = []
        for record in runtime_scope_imports(tree):
            if record.module in _FORBIDDEN_ADAPTER_ROOTS:
                violations.append(
                    f"  line {record.lineno}: {record.kind} {record.module}"
                )

        assert (
            violations == []
        ), "Conformance test uses package-root import(s):\n" + "\n".join(violations)

    def test_forbids_transport_package_root_import(self) -> None:
        """from medre.adapters.matrix import MatrixAdapter must be rejected."""
        source = "from medre.adapters.matrix import MatrixAdapter\n"
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in _FORBIDDEN_ADAPTER_ROOTS:
                    names = ", ".join(a.name for a in node.names)
                    violations.append(
                        f"line {node.lineno}: from {node.module} import {names}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_ADAPTER_ROOTS:
                        violations.append(f"line {node.lineno}: import {alias.name}")
        assert violations, "Should flag medre.adapters.matrix package-root import"

    def test_forbids_bare_import_adapter_root(self) -> None:
        """import medre.adapters.matrix must also be rejected."""
        source = "import medre.adapters.matrix\n"
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_ADAPTER_ROOTS:
                        violations.append(f"line {node.lineno}: import {alias.name}")
        assert violations, "Should flag bare import medre.adapters.matrix"

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

    def test_type_checking_else_branch_flagged(self) -> None:
        """Imports in TYPE_CHECKING else: branch are runtime-scope and flagged."""
        source = (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from medre.adapters.matrix import MatrixAdapter\n"
            "else:\n"
            "    from medre.adapters.matrix import MatrixAdapter\n"
        )
        tree = ast.parse(source)
        violations = [
            r
            for r in runtime_scope_imports(tree)
            if r.module in _FORBIDDEN_ADAPTER_ROOTS
        ]
        # The if-body import is excluded (TYPE_CHECKING); the else import
        # is runtime-scope and must be caught.
        assert (
            len(violations) >= 1
        ), "else-branch import of forbidden root must be flagged"
        assert any(
            r.lineno == 5 for r in violations
        ), "violation should be on line 5 (else branch)"
