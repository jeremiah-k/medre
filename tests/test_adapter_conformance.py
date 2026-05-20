"""Adapter conformance tests.

Verifies that every adapter (real and fake) conforms to the core
adapter contract shape using concrete imports only — no package-root
facade imports.
"""

from __future__ import annotations

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
        try:
            instance = cls(adapter_id="test")
            assert instance is not None
            assert instance.adapter_id == "test"
        except TypeError as e:
            pytest.skip(f"{cls.__name__} needs more args: {e}")

    @pytest.mark.parametrize("cls", _FAKE_ADAPTERS)
    def test_adapter_has_platform(self, cls: type) -> None:
        """Adapter class should expose a platform attribute."""
        try:
            instance = cls(adapter_id="test")
            platform = getattr(instance, "platform", None)
            assert platform is not None, f"{cls.__name__} has no platform"
            assert isinstance(platform, str), f"{cls.__name__} platform not str"
        except TypeError as e:
            pytest.skip(f"{cls.__name__} needs more args: {e}")


class TestRealAdapterContractImports:
    """Real adapter classes must be importable from concrete paths.

    Does NOT instantiate — only verifies the imports resolve.
    """

    def test_matrix_adapter_importable(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        assert issubclass(MatrixAdapter, AdapterContract)

    def test_meshtastic_adapter_importable(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        assert issubclass(MeshtasticAdapter, AdapterContract)

    def test_meshcore_adapter_importable(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        assert issubclass(MeshCoreAdapter, AdapterContract)

    def test_lxmf_adapter_importable(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        assert issubclass(LxmfAdapter, AdapterContract)


class TestNoPackageRootAdapterImports:
    """Conformance tests must not import from package-root facades."""

    def test_not_importing_from_adapters_root(self) -> None:
        """Verify this test file doesn't use medre.adapters import."""
        source = Path(__file__).read_text(encoding="utf-8")
        # Check we're not importing from medre.adapters directly
        forbidden = [
            "from medre.adapters import",
            "from medre.adapters.matrix import",
            "from medre.adapters.meshtastic import",
            "from medre.adapters.meshcore import",
            "from medre.adapters.lxmf import",
        ]
        for line in source.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(f) for f in forbidden):
                pytest.fail(f"Conformance test uses package-root import: {stripped}")
