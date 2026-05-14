"""Packaging, install, and import-boundary contract tests.

Tracks 2 & 10 — validates:

1. pyproject.toml metadata (name, version, entry point, extras, classifiers).
2. Base imports succeed without any optional transport SDK installed.
3. All fake adapters instantiate without optional SDKs.
4. Compat guards are present and typed as ``bool``.
5. RuntimeBuilder builds a multi-adapter runtime with ``adapter_kind="fake"``
   without any optional SDKs.
6. Documented extras match ``pyproject.toml``.

These tests are **not** marked ``live`` — they must pass in a bare
``pip install .`` environment with **no** ``[matrix]``, ``[meshtastic]``,
``[meshcore]``, or ``[lxmf]`` extras installed.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

from medre.config.paths import MedrePaths

# ---------------------------------------------------------------------------
# Path to pyproject.toml (repo root)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"


def _load_pyproject() -> dict[str, Any]:
    """Load and return parsed pyproject.toml."""
    with _PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


# ===================================================================
# 1. Package metadata
# ===================================================================


class TestPackageMetadata:
    """Verify pyproject.toml package metadata matches contract."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._project: dict[str, Any] = self._data["project"]

    def test_package_name_is_medre(self) -> None:
        assert self._project["name"] == "medre"

    def test_version_is_parseable(self) -> None:
        version = self._project["version"]
        # Not a full PEP 440 parse — just ensure it is non-empty and dotted.
        assert version, "version must not be empty"
        parts = version.split(".")
        assert len(parts) >= 2, f"version {version!r} should have ≥2 dot-separated parts"
        for part in parts:
            assert part.isdigit(), f"version segment {part!r} is not numeric"

    def test_console_script_entry_point(self) -> None:
        scripts = self._project.get("scripts", {})
        assert "medre" in scripts, (
            "pyproject.toml [project.scripts] missing 'medre' entry"
        )
        assert scripts["medre"] == "medre.cli:main", (
            f"expected 'medre.cli:main', got {scripts['medre']!r}"
        )

    def test_requires_python_gte_311(self) -> None:
        rp = self._project.get("requires-python", "")
        assert rp == ">=3.11", f"unexpected requires-python: {rp!r}"

    def test_license_declared(self) -> None:
        # License key must exist (even if governance-pending).
        assert "license" in self._project, "license key missing from pyproject.toml"

    def test_classifiers_include_beta(self) -> None:
        classifiers = self._project.get("classifiers", [])
        assert "Development Status :: 4 - Beta" in classifiers, (
            "classifiers missing 'Development Status :: 4 - Beta'"
        )

    def test_base_dependency_is_msgspec(self) -> None:
        deps = self._project.get("dependencies", [])
        assert any("msgspec" in d for d in deps), (
            f"msgspec not found in dependencies: {deps}"
        )

    def test_build_system_uses_setuptools(self) -> None:
        bs = self._data.get("build-system", {})
        requires = bs.get("requires", [])
        assert any("setuptools" in r for r in requires), (
            f"build-system does not require setuptools: {requires}"
        )


# ===================================================================
# 2. Optional extras
# ===================================================================


# The set of extras that **must** exist in pyproject.toml.
# Dev/test extras are optional (may exist); transport extras are required.
_REQUIRED_EXTRAS = frozenset({"matrix", "matrix-e2e", "meshtastic", "meshcore", "lxmf"})
_OPTIONAL_EXTRAS = frozenset({"dev", "test"})
_ALL_KNOWN_EXTRAS = _REQUIRED_EXTRAS | _OPTIONAL_EXTRAS


class TestOptionalExtras:
    """Verify optional-dependencies match the packaging contract."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._opt = _load_pyproject()["project"].get("optional-dependencies", {})

    def test_required_extras_present(self) -> None:
        missing = _REQUIRED_EXTRAS - set(self._opt.keys())
        assert not missing, f"missing required extras: {sorted(missing)}"

    def test_extras_are_lists_of_strings(self) -> None:
        for name, deps in self._opt.items():
            assert isinstance(deps, list), f"extra {name!r} is not a list"
            for dep in deps:
                assert isinstance(dep, str), f"extra {name!r} contains non-string: {dep!r}"

    def test_extras_do_not_overlap_base_deps(self) -> None:
        base = set(_load_pyproject()["project"].get("dependencies", []))
        for name, deps in self._opt.items():
            overlap = set(deps) & base
            assert not overlap, (
                f"extra {name!r} shares deps with base: {overlap}"
            )

    def test_matrix_extra_contains_nio(self) -> None:
        deps = self._opt.get("matrix", [])
        assert any("mindroom-nio" in d or "nio" in d for d in deps), (
            f"matrix extra missing mindroom-nio: {deps}"
        )

    def test_meshtastic_extra_contains_mtjk(self) -> None:
        deps = self._opt.get("meshtastic", [])
        assert any("mtjk" in d for d in deps), (
            f"meshtastic extra missing mtjk: {deps}"
        )

    def test_meshcore_extra_contains_meshcore(self) -> None:
        deps = self._opt.get("meshcore", [])
        assert any("meshcore" in d for d in deps), (
            f"meshcore extra missing meshcore: {deps}"
        )

    def test_lxmf_extra_contains_lxmf(self) -> None:
        deps = self._opt.get("lxmf", [])
        assert any("lxmf" in d for d in deps), (
            f"lxmf extra missing lxmf: {deps}"
        )

    def test_matrix_e2e_contains_e2e_marker(self) -> None:
        deps = self._opt.get("matrix-e2e", [])
        assert any("e2e" in d for d in deps), (
            f"matrix-e2e extra missing e2e marker: {deps}"
        )

    def test_no_unknown_transport_extras(self) -> None:
        """All extras in pyproject should be known to this test."""
        unknown = set(self._opt.keys()) - _ALL_KNOWN_EXTRAS
        assert not unknown, (
            f"unknown extras in pyproject.toml (add to this test): {sorted(unknown)}"
        )


# ===================================================================
# 3. Base import boundary (no optional SDKs)
# ===================================================================


class TestBaseImportBoundary:
    """Core imports must succeed without any optional transport SDK."""

    def test_import_medre(self) -> None:
        import medre  # noqa: F401
        assert medre is not None

    def test_import_medre_config(self) -> None:
        from medre.config import RuntimeConfig, load_config, MedrePaths
        assert RuntimeConfig is not None
        assert load_config is not None
        assert MedrePaths is not None

    def test_import_medre_runtime(self) -> None:
        from medre.runtime import (
            RuntimeError,
            RuntimeConfigError,
            MedreApp,
            RuntimeBuilder,
            AdapterBuildFailure,
        )
        assert RuntimeError is not None
        assert RuntimeBuilder is not None

    def test_import_medre_adapters_base(self) -> None:
        from medre.adapters import BaseAdapter, AdapterRole, AdapterCapabilities
        assert BaseAdapter is not None
        assert AdapterRole is not None

    def test_import_medre_cli(self) -> None:
        """CLI module imports must not pull in optional SDKs."""
        from medre.cli.main import _get_version
        assert callable(_get_version)


# ===================================================================
# 4. Fake adapters without SDKs
# ===================================================================


class TestFakeAdaptersWithoutSDKs:
    """All fake adapters must instantiate without optional SDKs."""

    def test_fake_transport_instantiation(self) -> None:
        from medre.adapters.fake_transport import FakeTransportAdapter
        adapter = FakeTransportAdapter("test_transport")
        assert adapter.adapter_id == "test_transport"

    def test_fake_matrix_instantiation(self) -> None:
        from medre.adapters.fake_matrix import FakeMatrixAdapter
        adapter = FakeMatrixAdapter("test_matrix")
        assert adapter.adapter_id == "test_matrix"

    def test_fake_meshtastic_instantiation(self) -> None:
        from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
        from medre.adapters.meshtastic.config import MeshtasticConfig
        config = MeshtasticConfig(adapter_id="test_mesh")
        adapter = FakeMeshtasticAdapter(config)
        assert adapter is not None

    def test_fake_meshcore_instantiation(self) -> None:
        from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
        from medre.adapters.meshcore.config import MeshCoreConfig
        config = MeshCoreConfig(adapter_id="test_meshcore")
        adapter = FakeMeshCoreAdapter(config)
        assert adapter is not None

    def test_fake_lxmf_instantiation(self) -> None:
        from medre.adapters.fake_lxmf import FakeLxmfAdapter
        from medre.adapters.lxmf.config import LxmfConfig
        config = LxmfConfig(adapter_id="test_lxmf")
        adapter = FakeLxmfAdapter(config)
        assert adapter is not None

    def test_fake_presentation_instantiation(self) -> None:
        from medre.adapters.fake_presentation import (
            FakePresentationAdapter,
            FaultyPresentationAdapter,
        )
        adapter = FakePresentationAdapter("test_pres")
        assert adapter.adapter_id == "test_pres"
        faulty = FaultyPresentationAdapter("test_faulty")
        assert faulty is not None

    def test_all_fakes_importable_from_adapters_init(self) -> None:
        from medre.adapters import (
            FakeTransportAdapter,
            FakeMatrixAdapter,
            FakeMeshtasticAdapter,
            FakeMeshCoreAdapter,
            FakeLxmfAdapter,
            FakePresentationAdapter,
        )
        assert all([
            FakeTransportAdapter,
            FakeMatrixAdapter,
            FakeMeshtasticAdapter,
            FakeMeshCoreAdapter,
            FakeLxmfAdapter,
            FakePresentationAdapter,
        ])


# ===================================================================
# 5. Compat guard flags
# ===================================================================


class TestCompatGuards:
    """Compat guard modules must import safely and expose bool flags."""

    def test_matrix_compat_has_nio_is_bool(self) -> None:
        from medre.adapters.matrix.compat import HAS_NIO
        assert isinstance(HAS_NIO, bool)

    def test_matrix_compat_has_e2ee_is_bool(self) -> None:
        from medre.adapters.matrix.compat import HAS_E2EE
        assert isinstance(HAS_E2EE, bool)

    def test_meshtastic_compat_has_meshtastic_is_bool(self) -> None:
        from medre.adapters.meshtastic.compat import HAS_MESHTASTIC
        assert isinstance(HAS_MESHTASTIC, bool)

    def test_meshcore_compat_has_meshcore_is_bool(self) -> None:
        from medre.adapters.meshcore.compat import HAS_MESHCORE
        assert isinstance(HAS_MESHCORE, bool)

    def test_lxmf_compat_has_lxmf_is_bool(self) -> None:
        from medre.adapters.lxmf.compat import HAS_LXMF
        assert isinstance(HAS_LXMF, bool)


# ===================================================================
# 6. RuntimeBuilder with fake multi-adapter config (no SDKs)
# ===================================================================


class TestRuntimeBuilderWithFakeMultiAdapter:
    """RuntimeBuilder must construct a multi-adapter runtime using
    ``adapter_kind="fake"`` without any optional transport SDKs."""

    @pytest.fixture(autouse=True)
    def _clean_path_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
            "XDG_DATA_HOME", "XDG_CACHE_HOME",
        ):
            monkeypatch.delenv(var, raising=False)

    @pytest.fixture()
    def tmp_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        from medre.config.paths import resolve
        return resolve()

    def test_build_all_four_transport_fakes(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Build a runtime with all four transports in fake mode."""
        from medre.config.model import (
            AdapterConfigSet,
            LoggingConfig,
            LxmfRuntimeConfig,
            MatrixRuntimeConfig,
            MeshCoreRuntimeConfig,
            MeshtasticRuntimeConfig,
            RuntimeConfig,
            RuntimeLimits,
            RuntimeOptions,
            StorageConfig,
        )
        from medre.runtime.builder import RuntimeBuilder

        adapters = AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "radio": MeshtasticRuntimeConfig(
                    adapter_id="fake_mesh",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshcore={
                "node": MeshCoreRuntimeConfig(
                    adapter_id="fake_meshcore",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            lxmf={
                "local": LxmfRuntimeConfig(
                    adapter_id="fake_lxmf",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        )

        config = RuntimeConfig(
            runtime=RuntimeOptions(name="packaging-test"),
            logging=LoggingConfig(level="DEBUG"),
            storage=StorageConfig(backend="memory"),
            limits=RuntimeLimits(),
            adapters=adapters,
        )

        app = RuntimeBuilder(config, tmp_paths).build()
        assert app is not None

        # All four fake adapters must be present.
        assert len(app.adapters) == 4, (
            f"expected 4 adapters, got {len(app.adapters)}: "
            f"{sorted(app.adapters.keys())}"
        )
        # Verify each adapter was built (dict keys are the adapter_id from config).
        expected_keys = {"fake_matrix", "fake_mesh", "fake_meshcore", "fake_lxmf"}
        assert set(app.adapters.keys()) == expected_keys, (
            f"adapter key mismatch: got {sorted(app.adapters.keys())}, "
            f"want {sorted(expected_keys)}"
        )

    def test_build_no_adapters(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Builder works with zero adapters (bare install)."""
        from medre.config.model import RuntimeConfig, StorageConfig
        from medre.runtime.builder import RuntimeBuilder

        config = RuntimeConfig(storage=StorageConfig(backend="memory"))
        app = RuntimeBuilder(config, tmp_paths).build()
        assert app is not None
        assert len(app.adapters) == 0


# ===================================================================
# 7. Docs contract consistency — extras match pyproject
# ===================================================================

# This section validates that the packaging contract document and pyproject
# extras are consistent. The contract doc at docs/contracts/58 references
# the same extras listed here.


class TestDocsContractConsistency:
    """Cross-check documented extras against pyproject.toml."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._opt = _load_pyproject()["project"].get("optional-dependencies", {})

    def test_contract_doc_exists(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "contracts" / "58-packaging-and-install-contract.md"
        assert contract_path.is_file(), (
            f"Packaging contract doc missing: {contract_path}"
        )

    def test_contract_doc_mentions_all_extras(self) -> None:
        contract_path = (
            _REPO_ROOT / "docs" / "contracts" / "58-packaging-and-install-contract.md"
        )
        content = contract_path.read_text()
        for extra_name in _REQUIRED_EXTRAS:
            assert extra_name in content, (
                f"Contract doc does not mention extra {extra_name!r}"
            )
