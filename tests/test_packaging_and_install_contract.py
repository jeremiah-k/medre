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

import os
import subprocess
import sys
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
        assert (
            len(parts) >= 2
        ), f"version {version!r} should have ≥2 dot-separated parts"
        for part in parts:
            assert part.isdigit(), f"version segment {part!r} is not numeric"

    def test_console_script_entry_point(self) -> None:
        scripts = self._project.get("scripts", {})
        assert (
            "medre" in scripts
        ), "pyproject.toml [project.scripts] missing 'medre' entry"
        assert (
            scripts["medre"] == "medre.cli:main"
        ), f"expected 'medre.cli:main', got {scripts['medre']!r}"

    def test_requires_python_gte_311(self) -> None:
        rp = self._project.get("requires-python", "")
        assert rp == ">=3.11", f"unexpected requires-python: {rp!r}"

    def test_license_declared(self) -> None:
        # License key must exist (even if governance-pending).
        assert "license" in self._project, "license key missing from pyproject.toml"

    def test_classifiers_include_alpha(self) -> None:
        classifiers = self._project.get("classifiers", [])
        assert (
            "Development Status :: 3 - Alpha" in classifiers
        ), "classifiers missing 'Development Status :: 3 - Alpha'"

    def test_base_dependency_is_msgspec(self) -> None:
        deps = self._project.get("dependencies", [])
        assert any(
            "msgspec" in d for d in deps
        ), f"msgspec not found in dependencies: {deps}"

    def test_build_system_uses_setuptools(self) -> None:
        bs = self._data.get("build-system", {})
        requires = bs.get("requires", [])
        assert any(
            "setuptools" in r for r in requires
        ), f"build-system does not require setuptools: {requires}"


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
                assert isinstance(
                    dep, str
                ), f"extra {name!r} contains non-string: {dep!r}"

    def test_extras_do_not_overlap_base_deps(self) -> None:
        base = set(_load_pyproject()["project"].get("dependencies", []))
        for name, deps in self._opt.items():
            overlap = set(deps) & base
            assert not overlap, f"extra {name!r} shares deps with base: {overlap}"

    def test_matrix_extra_contains_nio(self) -> None:
        deps = self._opt.get("matrix", [])
        assert any(
            "mindroom-nio" in d or "nio" in d for d in deps
        ), f"matrix extra missing mindroom-nio: {deps}"

    def test_meshtastic_extra_contains_mtjk(self) -> None:
        deps = self._opt.get("meshtastic", [])
        assert any("mtjk" in d for d in deps), f"meshtastic extra missing mtjk: {deps}"

    def test_meshcore_extra_contains_meshcore(self) -> None:
        deps = self._opt.get("meshcore", [])
        assert any(
            "meshcore" in d for d in deps
        ), f"meshcore extra missing meshcore: {deps}"

    def test_lxmf_extra_contains_lxmf(self) -> None:
        deps = self._opt.get("lxmf", [])
        assert any("lxmf" in d for d in deps), f"lxmf extra missing lxmf: {deps}"

    def test_matrix_e2e_contains_e2e_marker(self) -> None:
        deps = self._opt.get("matrix-e2e", [])
        assert any(
            "e2e" in d for d in deps
        ), f"matrix-e2e extra missing e2e marker: {deps}"

    def test_no_unknown_transport_extras(self) -> None:
        """All extras in pyproject should be known to this test."""
        unknown = set(self._opt.keys()) - _ALL_KNOWN_EXTRAS
        assert (
            not unknown
        ), f"unknown extras in pyproject.toml (add to this test): {sorted(unknown)}"


# ===================================================================
# 3. Base import boundary (no optional SDKs)
# ===================================================================


class TestBaseImportBoundary:
    """Core imports must succeed without any optional transport SDK."""

    def test_import_medre(self) -> None:
        import medre  # noqa: F401

        assert medre is not None

    def test_import_medre_config(self) -> None:
        from medre.config.loader import load_config
        from medre.config.model import RuntimeConfig
        from medre.config.paths import MedrePaths

        assert RuntimeConfig is not None
        assert load_config is not None
        assert MedrePaths is not None

    def test_import_medre_runtime(self) -> None:
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.errors import RuntimeError as MedreRuntimeError

        assert MedreRuntimeError is not None
        assert RuntimeBuilder is not None

    def test_import_medre_core_adapter_contracts(self) -> None:
        from medre.core.contracts.adapter import (
            AdapterContract,
            AdapterRole,
        )

        assert AdapterContract is not None
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
        from medre.adapters.fakes.transport import FakeTransportAdapter

        adapter = FakeTransportAdapter("test_transport")
        assert adapter.adapter_id == "test_transport"

    def test_fake_matrix_instantiation(self) -> None:
        from medre.adapters.fakes.matrix import FakeMatrixAdapter

        adapter = FakeMatrixAdapter("test_matrix")
        assert adapter.adapter_id == "test_matrix"

    def test_fake_meshtastic_instantiation(self) -> None:
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test_mesh")
        adapter = FakeMeshtasticAdapter(config)
        assert adapter is not None

    def test_fake_meshcore_instantiation(self) -> None:
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test_meshcore")
        adapter = FakeMeshCoreAdapter(config)
        assert adapter is not None

    def test_fake_lxmf_instantiation(self) -> None:
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(adapter_id="test_lxmf")
        adapter = FakeLxmfAdapter(config)
        assert adapter is not None

    def test_fake_presentation_instantiation(self) -> None:
        from medre.adapters.fakes.presentation import (
            FakePresentationAdapter,
            FaultyPresentationAdapter,
        )

        adapter = FakePresentationAdapter("test_pres")
        assert adapter.adapter_id == "test_pres"
        faulty = FaultyPresentationAdapter("test_faulty")
        assert faulty is not None

    def test_all_fakes_importable_from_submodules(self) -> None:
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter
        from medre.adapters.fakes.matrix import FakeMatrixAdapter
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.adapters.fakes.presentation import FakePresentationAdapter
        from medre.adapters.fakes.transport import FakeTransportAdapter

        assert all(
            [
                FakeTransportAdapter,
                FakeMatrixAdapter,
                FakeMeshtasticAdapter,
                FakeMeshCoreAdapter,
                FakeLxmfAdapter,
                FakePresentationAdapter,
            ]
        )


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
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            monkeypatch.delenv(var, raising=False)

    @pytest.fixture()
    def tmp_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        from medre.config.paths import resolve

        return resolve()

    def test_build_all_four_transport_fakes(self, tmp_paths: MedrePaths) -> None:
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

    def test_build_no_adapters(self, tmp_paths: MedrePaths) -> None:
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

# This section validates that the packaging spec document and pyproject
# extras are consistent. The spec doc at docs/spec references
# the same extras listed here.


class TestDocsContractConsistency:
    """Cross-check documented extras against pyproject.toml."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._opt = _load_pyproject()["project"].get("optional-dependencies", {})

    def test_contract_doc_exists(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "ops" / "install.md"
        assert (
            contract_path.is_file()
        ), f"Packaging contract doc missing: {contract_path}"

    def test_contract_doc_mentions_all_extras(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "ops" / "install.md"
        content = contract_path.read_text()
        for extra_name in _REQUIRED_EXTRAS:
            assert (
                extra_name in content
            ), f"Contract doc does not mention extra {extra_name!r}"


# ===================================================================
# 8. py.typed marker (PEP 561)
# ===================================================================


class TestPyTypedMarker:
    """The py.typed marker must be shipped alongside the Typing :: Typed classifier."""

    def test_py_typed_file_exists_in_src(self) -> None:
        marker = _REPO_ROOT / "src" / "medre" / "py.typed"
        assert marker.is_file(), (
            f"py.typed marker missing at {marker} — required by PEP 561 "
            "and the 'Typing :: Typed' classifier"
        )

    def test_py_typed_file_is_empty(self) -> None:
        """PEP 561 partial stub packages may have content, but for a
        fully-typed package the marker is typically empty."""
        marker = _REPO_ROOT / "src" / "medre" / "py.typed"
        content = marker.read_text().strip()
        # Allow empty or just whitespace; must not have import stubs.
        assert not content or content.startswith(
            "#"
        ), f"py.typed has unexpected content: {content!r}"

    def test_typed_classifier_matches_marker(self) -> None:
        """If 'Typing :: Typed' is declared, py.typed must exist."""
        classifiers = _load_pyproject()["project"].get("classifiers", [])
        has_typed_classifier = "Typing :: Typed" in classifiers
        has_marker = (_REPO_ROOT / "src" / "medre" / "py.typed").is_file()
        assert (
            has_typed_classifier == has_marker
        ), f"classifier={has_typed_classifier}, marker={has_marker} — must match"


# ===================================================================
# 9. python -m medre delegation
# ===================================================================


class TestPythonMModule:
    """``python -m medre`` delegates to the canonical CLI entry point."""

    def test_main_module_exists(self) -> None:
        """``src/medre/__main__.py`` must exist."""
        main_mod = _REPO_ROOT / "src" / "medre" / "__main__.py"
        assert main_mod.is_file(), f"__main__.py missing at {main_mod}"

    def test_main_module_delegates_to_cli(self) -> None:
        """__main__.py must import from medre.cli."""
        main_mod = _REPO_ROOT / "src" / "medre" / "__main__.py"
        content = main_mod.read_text()
        assert "medre.cli" in content, "__main__.py must delegate to medre.cli"

    def test_main_module_importable(self) -> None:
        """``python -m medre`` resolves to __main__.py that delegates to CLI.

        We verify the module source is well-formed rather than importing it
        directly, because importing triggers ``main()`` which would consume
        sys.argv.
        """
        import importlib.util

        main_mod = _REPO_ROOT / "src" / "medre" / "__main__.py"
        spec = importlib.util.spec_from_file_location("medre.__main__", main_mod)
        assert spec is not None, "could not create module spec for __main__.py"
        # Verify the source references medre.cli.
        source = main_mod.read_text()
        assert "medre.cli" in source

    def test_import_medre_cli_main_callable(self) -> None:
        """``medre.cli.main`` is a callable entry point."""
        from medre.cli import main

        assert callable(main)


# ===================================================================
# 10. CLI help and config-check without optional SDK imports
# ===================================================================


# Optional SDK module names that must NOT appear in sys.modules after
# importing medre.core / medre.cli / medre.adapters (fake path only).
_OPTIONAL_SDK_MODULES = frozenset(
    {
        "nio",
        "meshtastic",
        "meshcore",
        "RNS",
        "LXMF",
    }
)


class TestCLIHelpWithoutSDKs:
    """CLI help, config check, and version must not import optional SDKs."""

    def test_help_without_optional_sdks(self) -> None:
        """``main(["--help"])`` succeeds without importing optional SDKs.

        We verify that none of the optional SDK modules appear in
        sys.modules after the help invocation.
        """
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        # Record which optional modules are present before the call.
        before = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(["--help"])
        except SystemExit as exc:
            # --help exits with code 0.
            assert exc.code in (None, 0), f"unexpected exit code: {exc.code}"

        after = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}
        leaked = after - before
        assert (
            not leaked
        ), f"optional SDK modules leaked into sys.modules by --help: {sorted(leaked)}"

    def test_config_check_fake_without_optional_sdks(self) -> None:
        """``config check`` with a fake-only config succeeds without SDKs."""
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        # Clean env.
        for var in (
            "MEDRE_HOME",
            "MEDRE_CONFIG",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            os.environ.pop(var, None)

        # First generate a sample config.
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(["config", "sample"])
        except SystemExit:
            pass
        sample = stdout.getvalue()

        # Write it to a temp file and check it.
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tf:
            tf.write(sample)
            config_path = tf.name

        try:
            before = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}

            stdout2 = io.StringIO()
            stderr2 = io.StringIO()
            try:
                with redirect_stdout(stdout2), redirect_stderr(stderr2):
                    main(["config", "check", "--config", config_path])
            except SystemExit:
                # config check may succeed or fail depending on sample content;
                # the key requirement is no SDK imports.
                pass

            after = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}
            leaked = after - before
            assert (
                not leaked
            ), f"optional SDK modules leaked by config check: {sorted(leaked)}"
        finally:
            os.unlink(config_path)

    def test_version_without_optional_sdks(self) -> None:
        """``main(["version"])`` succeeds without importing optional SDKs."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        before = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(["version"])
        except SystemExit:
            pass

        after = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}
        leaked = after - before
        assert not leaked, f"optional SDK modules leaked by version: {sorted(leaked)}"

    def test_adapters_without_optional_sdks(self) -> None:
        """``main(["adapters"])`` succeeds without importing optional SDKs."""
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        for var in (
            "MEDRE_HOME",
            "MEDRE_CONFIG",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            os.environ.pop(var, None)

        before = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(["adapters"])
        except SystemExit:
            pass

        after = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}
        leaked = after - before
        assert not leaked, f"optional SDK modules leaked by adapters: {sorted(leaked)}"


# ===================================================================
# 11. Fake adapters do not transitively import optional SDKs
# ===================================================================


class TestFakeAdaptersNoTransitiveSDKImports:
    """Importing fake adapters must not transitively import optional SDKs."""

    def test_fake_matrix_no_nio_import(self) -> None:
        before = "nio" in sys.modules
        from medre.adapters.fakes.matrix import FakeMatrixAdapter  # noqa: F401

        after = "nio" in sys.modules
        assert (
            before == after
        ), "importing FakeMatrixAdapter leaked 'nio' into sys.modules"

    def test_fake_meshtastic_no_sdk_import(self) -> None:
        before = "meshtastic" in sys.modules
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter  # noqa: F401

        after = "meshtastic" in sys.modules
        assert (
            before == after
        ), "importing FakeMeshtasticAdapter leaked 'meshtastic' into sys.modules"

    def test_fake_meshcore_no_sdk_import(self) -> None:
        before = "meshcore" in sys.modules
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter  # noqa: F401

        after = "meshcore" in sys.modules
        assert (
            before == after
        ), "importing FakeMeshCoreAdapter leaked 'meshcore' into sys.modules"

    def test_fake_lxmf_no_sdk_import(self) -> None:
        before_rns = "RNS" in sys.modules
        before_lxmf = "LXMF" in sys.modules
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter  # noqa: F401

        after_rns = "RNS" in sys.modules
        after_lxmf = "LXMF" in sys.modules
        assert (
            before_rns == after_rns
        ), "importing FakeLxmfAdapter leaked 'RNS' into sys.modules"
        assert (
            before_lxmf == after_lxmf
        ), "importing FakeLxmfAdapter leaked 'LXMF' into sys.modules"


# ===================================================================
# 12. Missing SDK error messages mention medre[extra] install hints
# ===================================================================


class TestMissingSDKErrorMessages:
    """Error messages for missing optional SDKs must mention the install extra."""

    def _get_source(self, relative_path: str) -> str:
        return (_REPO_ROOT / relative_path).read_text()

    def test_matrix_adapter_mentions_medre_matrix(self) -> None:
        src = self._get_source("src/medre/adapters/matrix/adapter.py")
        assert (
            "medre[matrix]" in src
        ), "Matrix adapter error message should mention pip install 'medre[matrix]'"

    def test_matrix_session_e2ee_mentions_medre_matrix_e2e(self) -> None:
        src = self._get_source("src/medre/adapters/matrix/session.py")
        assert (
            "medre[matrix-e2e]" in src
        ), "Matrix session E2EE error should mention pip install 'medre[matrix-e2e]'"

    def test_meshtastic_session_mentions_medre_meshtastic(self) -> None:
        src = self._get_source("src/medre/adapters/meshtastic/session.py")
        assert (
            "medre[meshtastic]" in src
        ), "Meshtastic session error should mention pip install 'medre[meshtastic]'"

    def test_meshcore_session_mentions_medre_meshcore(self) -> None:
        src = self._get_source("src/medre/adapters/meshcore/session.py")
        assert (
            "medre[meshcore]" in src
        ), "MeshCore session error should mention pip install 'medre[meshcore]'"

    def test_lxmf_adapter_mentions_medre_lxmf(self) -> None:
        src = self._get_source("src/medre/adapters/lxmf/adapter.py")
        assert (
            "medre[lxmf]" in src
        ), "LXMF adapter error should mention pip install 'medre[lxmf]'"

    def test_lxmf_session_mentions_medre_lxmf(self) -> None:
        src = self._get_source("src/medre/adapters/lxmf/session.py")
        assert (
            "medre[lxmf]" in src
        ), "LXMF session error should mention pip install 'medre[lxmf]'"

    def test_lxmf_compat_mentions_medre_lxmf(self) -> None:
        src = self._get_source("src/medre/adapters/lxmf/compat.py")
        assert (
            "medre[lxmf]" in src
        ), "LXMF compat error should mention pip install 'medre[lxmf]'"

    def test_smoke_fake_path_no_sdk_import(self) -> None:
        """Smoke with a fake-only config must not import optional SDKs.

        This runs the smoke command with the default fake-only config
        to verify that the smoke path works without SDKs.
        """
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout

        from medre.cli import main

        for var in (
            "MEDRE_HOME",
            "MEDRE_CONFIG",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        ):
            os.environ.pop(var, None)

        before = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}

        # Use the fake bridge smoke config shipped in examples.
        smoke_config = _REPO_ROOT / "examples" / "configs" / "fake-bridge-smoke.yaml"
        if not smoke_config.is_file():
            pytest.skip("fake-bridge-smoke.yaml not found")

        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(["smoke", "--config", str(smoke_config)])
        except SystemExit:
            pass

        after = {m for m in _OPTIONAL_SDK_MODULES if m in sys.modules}
        leaked = after - before
        assert (
            not leaked
        ), f"optional SDK modules leaked by smoke fake path: {sorted(leaked)}"


# ===================================================================
# 13. Contract metadata alignment
# ===================================================================


class TestContractMetadataAlignment:
    """Contract 58 metadata must match pyproject.toml."""

    def test_contract_license_matches_pyproject(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "ops" / "install.md"
        content = contract_path.read_text()
        project = _load_pyproject()["project"]
        license_val = project.get("license", "")
        assert (
            license_val in content
        ), f"pyproject license {license_val!r} not mentioned in spec"

    def test_contract_classifier_matches_pyproject(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "ops" / "install.md"
        content = contract_path.read_text()
        classifiers = _load_pyproject()["project"].get("classifiers", [])
        # Find the Development Status classifier.
        status_clf = [c for c in classifiers if "Development Status" in c]
        assert status_clf, "no Development Status classifier in pyproject"
        assert (
            status_clf[0] in content
        ), f"classifier {status_clf[0]!r} not mentioned in spec"

    def test_console_script_in_contract(self) -> None:
        contract_path = _REPO_ROOT / "docs" / "ops" / "install.md"
        content = contract_path.read_text()
        assert (
            "medre.cli:main" in content
        ), "spec must mention the canonical entry point medre.cli:main"


# ===================================================================
# 14. Explicit py.typed package-data config
# ===================================================================


class TestPackageDataConfig:
    """Verify [tool.setuptools.package-data] explicitly includes py.typed.

    setuptools >= 69 auto-includes py.typed only in experimental mode.
    MEDRE requires setuptools >= 68, so the explicit config is required
    to guarantee py.typed ships in the wheel regardless of setuptools version.
    """

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()

    def test_package_data_section_exists(self) -> None:
        """[tool.setuptools.package-data] must be present in pyproject.toml."""
        pkg_data = self._data.get("tool", {}).get("setuptools", {}).get("package-data")
        assert (
            pkg_data is not None
        ), "pyproject.toml missing [tool.setuptools.package-data] section"

    def test_medre_package_data_includes_py_typed(self) -> None:
        """medre package must list py.typed in its package-data."""
        pkg_data = (
            self._data.get("tool", {}).get("setuptools", {}).get("package-data", {})
        )
        assert "medre" in pkg_data, "[tool.setuptools.package-data] missing 'medre' key"
        medre_files = pkg_data["medre"]
        assert "py.typed" in medre_files, (
            f"[tool.setuptools.package-data] medre does not list 'py.typed': "
            f"{medre_files}"
        )

    def test_py_typed_source_file_matches_config(self) -> None:
        """The py.typed file referenced in config must exist on disk."""
        marker = _REPO_ROOT / "src" / "medre" / "py.typed"
        assert marker.is_file(), f"py.typed source file missing at {marker}"


# ===================================================================
# 15. CLI subpackage __main__.py
# ===================================================================


class TestCliMainModule:
    """``python -m medre.cli`` must be supported by ``cli/__main__.py``."""

    def test_cli_main_module_exists(self) -> None:
        cli_main = _REPO_ROOT / "src" / "medre" / "cli" / "__main__.py"
        assert cli_main.is_file(), f"cli/__main__.py missing at {cli_main}"

    def test_cli_main_module_delegates(self) -> None:
        cli_main = _REPO_ROOT / "src" / "medre" / "cli" / "__main__.py"
        content = cli_main.read_text()
        assert "medre.cli" in content, "cli/__main__.py must delegate to medre.cli"


# ===================================================================
# 16. Wheel artifact contract (opt-in via ``build`` availability)
# ===================================================================


class TestWheelArtifactContract:
    """Build a wheel with ``--no-isolation`` and inspect its contents.

    These tests require the ``build`` package. They are skipped
    automatically when ``build`` is not installed so the default
    test suite remains fast and network-free.
    """

    @pytest.fixture(autouse=True)
    def _check_build(self) -> None:
        try:
            import build  # noqa: F401
        except ImportError:
            pytest.skip("python 'build' package not installed")

    @pytest.fixture(scope="module")
    def _wheel_path(self: "TestWheelArtifactContract") -> object:
        """Build wheel once per class and return its path."""
        import tempfile

        import build

        with tempfile.TemporaryDirectory(prefix="medre-wheel-test-") as tmpdir:
            builder = build.ProjectBuilder(_REPO_ROOT)
            wheel = builder.build(
                distribution="wheel",
                output_directory=tmpdir,
                config_settings={"--no-isolation": ""},
            )
            yield Path(wheel)

    def _wheel_names(self, wheel_path: Path) -> set[str]:
        """Return the set of file names inside the wheel."""
        import zipfile

        with zipfile.ZipFile(wheel_path) as zf:
            return set(zf.namelist())

    def test_wheel_contains_py_typed(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        assert "medre/py.typed" in names, (
            f"wheel missing medre/py.typed. Found py.typed entries: "
            f"{sorted(n for n in names if 'py.typed' in n)}"
        )

    def test_wheel_contains_main_module(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        assert "medre/__main__.py" in names, "wheel missing medre/__main__.py"

    def test_wheel_contains_cli_main_module(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        assert "medre/cli/__main__.py" in names, "wheel missing medre/cli/__main__.py"

    def test_wheel_excludes_tests(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        test_files = [n for n in names if n.startswith("tests/") or "/tests/" in n]
        assert not test_files, f"wheel should not contain tests/: {sorted(test_files)}"

    def test_wheel_excludes_docs(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        doc_files = [n for n in names if n.startswith("docs/") or "/docs/" in n]
        assert not doc_files, f"wheel should not contain docs/: {sorted(doc_files)}"

    def test_wheel_excludes_examples(self, _wheel_path: Path) -> None:
        names = self._wheel_names(_wheel_path)
        example_files = [
            n for n in names if n.startswith("examples/") or "/examples/" in n
        ]
        assert (
            not example_files
        ), f"wheel should not contain examples/: {sorted(example_files)}"


# ===================================================================
# 17. python -m medre / python -m medre.cli subprocess invocation
# ===================================================================


class TestPythonMInvocation:
    """``python -m medre`` and ``python -m medre.cli`` must work as
    subprocess invocations, matching the installed-package contract.

    These tests verify that the ``__main__.py`` delegation files produce
    correct exit codes and output when invoked via ``python -m`` — the
    primary entry point for users who installed the package (as opposed
    to running the ``medre`` console-script).
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "MEDRE_CONFIG",
        ):
            monkeypatch.delenv(var, raising=False)

    def _run_module(self, module: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``python -m <module> [args...]`` and return the result."""
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
        )

    def test_python_m_medre_help_exits_zero(self) -> None:
        """``python -m medre --help`` exits 0 and prints usage."""
        result = self._run_module("medre", "--help")
        assert result.returncode == 0, (
            f"python -m medre --help exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        assert (
            "usage:" in result.stdout.lower() or "medre" in result.stdout.lower()
        ), f"Expected usage text in stdout: {result.stdout[:200]!r}"

    def test_python_m_medre_cli_help_exits_zero(self) -> None:
        """``python -m medre.cli --help`` exits 0 and prints usage."""
        result = self._run_module("medre.cli", "--help")
        assert result.returncode == 0, (
            f"python -m medre.cli --help exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        assert (
            "usage:" in result.stdout.lower() or "medre" in result.stdout.lower()
        ), f"Expected usage text in stdout: {result.stdout[:200]!r}"

    def test_python_m_medre_version(self) -> None:
        """``python -m medre version`` prints version info."""
        result = self._run_module("medre", "version")
        assert result.returncode == 0, (
            f"python -m medre version exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        assert (
            "medre" in result.stdout.lower()
        ), f"Expected 'medre' in version output: {result.stdout!r}"

    def test_python_m_medre_cli_version(self) -> None:
        """``python -m medre.cli version`` prints version info."""
        result = self._run_module("medre.cli", "version")
        assert result.returncode == 0, (
            f"python -m medre.cli version exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        assert (
            "medre" in result.stdout.lower()
        ), f"Expected 'medre' in version output: {result.stdout!r}"

    def test_python_m_medre_config_sample(self) -> None:
        """``python -m medre config sample`` produces valid YAML."""
        from medre.config._yaml import parse_yaml_config

        result = self._run_module("medre", "config", "sample")
        assert result.returncode == 0, (
            f"python -m medre config sample exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        parsed = parse_yaml_config(result.stdout)
        assert (
            "runtime" in parsed
        ), f"Sample output missing 'runtime': {list(parsed.keys())}"
        assert (
            "adapters" in parsed
        ), f"Sample output missing 'adapters': {list(parsed.keys())}"

    def test_python_m_medre_paths(self) -> None:
        """``python -m medre paths`` prints path information."""
        result = self._run_module("medre", "paths")
        assert result.returncode == 0, (
            f"python -m medre paths exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        assert (
            "medre" in result.stdout.lower()
        ), f"Expected 'medre' in paths output: {result.stdout!r}"

    def test_python_m_medre_adapters(self) -> None:
        """``python -m medre adapters`` lists adapter types."""
        result = self._run_module("medre", "adapters")
        assert result.returncode == 0, (
            f"python -m medre adapters exited {result.returncode}: "
            f"stderr={result.stderr!r}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert (
            "matrix" in combined or "adapter" in combined
        ), f"Expected adapter info in output: {combined[:200]!r}"
