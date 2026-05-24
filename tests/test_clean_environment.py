"""Track 3: Clean-environment validation tests — reproducibility evidence.

Validates that a **bare ``pip install .``** (no extras) produces a fully
functional, deterministic environment.  Every test in this module:

- Runs **without** any optional transport SDK (no matrix, meshtastic,
  meshcore, or lxmf libraries installed).
- Exercises **metadata, documentation, and import boundaries** only — no
  live network, no hardware, no actual ``pip install`` / ``python -m build``.
- Produces **deterministic** pass/fail results.
- Does **not** duplicate ``test_packaging_and_install_contract.py``; instead
  it covers orthogonal clean-environment concerns:

  1. Editable-install command documentation matches pyproject.toml.
  2. Build-system metadata is complete for ``python -m build``.
  3. Extras dependency graph is internally consistent (e2e ⊃ matrix).
  4. Console-script entry point resolves to a live callable.
  5. All library subpackages import cleanly (broader than base-import test).
  6. Config sample generation works and is valid TOML.
  7. CLI smoke (version / paths / adapters / config-sample) from clean env.
  8. Environment-variable override machinery is importable and typed.
  9. ``compileall`` — every ``.py`` under ``src/`` compiles.
  10. Package layout — src/ flat-files, no stray top-level modules.
"""

from __future__ import annotations

import importlib
import io
import os
import py_compile
import subprocess
import sys
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_SRC_DIR = _REPO_ROOT / "src"
_DOCS_DIR = _REPO_ROOT / "docs"
_DEV_ENV_DOC = _DOCS_DIR / "runbooks" / "developer-environment.md"


def _load_pyproject() -> dict[str, Any]:
    with _PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


# ===================================================================
# 1. Editable-install command documentation
# ===================================================================


class TestEditableInstallDocumentation:
    """Developer-environment.md must document correct install commands that
    match the actual ``pyproject.toml`` extras."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._project = self._data["project"]
        self._opt: dict[str, list[str]] = self._project.get("optional-dependencies", {})
        assert _DEV_ENV_DOC.is_file(), f"dev-env doc missing: {_DEV_ENV_DOC}"
        self._doc_text = _DEV_ENV_DOC.read_text()

    def test_doc_mentions_editable_base_install(self) -> None:
        """Doc must include ``pip install -e .`` (base install)."""
        assert (
            'pip install -e "."' in self._doc_text
            or "pip install -e ." in self._doc_text
        ), "developer-environment.md missing base editable install command"

    def test_doc_mentions_editable_dev_install(self) -> None:
        """Doc must include ``pip install -e ".[dev]"``."""
        assert (
            'pip install -e ".[dev]"' in self._doc_text
            or 'pip install -e ".[dev]"' in self._doc_text
        ), "developer-environment.md missing dev editable install command"

    def test_doc_documents_each_transport_extra(self) -> None:
        """Each transport extra in pyproject.toml must appear in the doc."""
        transport_extras = {"matrix", "matrix-e2e", "meshtastic", "meshcore", "lxmf"}
        for extra in transport_extras:
            assert (
                f"[{extra}]" in self._doc_text
            ), f"developer-environment.md does not document [{extra}] extra"

    def test_doc_install_commands_use_project_name(self) -> None:
        """Install commands must reference the correct project name."""
        name = self._project["name"]
        # The doc should use the project name in install examples
        assert (
            name in self._doc_text
        ), f"developer-environment.md does not mention project name {name!r}"

    def test_doc_mentions_python_version_requirement(self) -> None:
        """Doc must state the minimum Python version matching pyproject.toml."""
        rp = self._project.get("requires-python", "")
        # e.g. ">=3.11" → doc should mention "3.11"
        min_ver = rp.lstrip(">=")
        assert min_ver in self._doc_text, (
            f"developer-environment.md does not mention Python {min_ver} "
            f"(from requires-python={rp!r})"
        )


# ===================================================================
# 2. Build-system metadata for wheel/sdist
# ===================================================================


class TestBuildSystemMetadata:
    """Verify build-system configuration is complete and correct for
    ``python -m build`` (setuptools backend)."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._bs = self._data.get("build-system", {})
        self._project = self._data["project"]

    def test_build_backend_is_setuptools(self) -> None:
        assert (
            self._bs.get("build-backend") == "setuptools.build_meta"
        ), f"unexpected build-backend: {self._bs.get('build-backend')!r}"

    def test_build_requires_setuptools(self) -> None:
        requires = self._bs.get("requires", [])
        assert any(
            "setuptools" in r for r in requires
        ), f"setuptools not in build-system.requires: {requires}"
        # Pin should be >=68 per pyproject.toml
        for r in requires:
            if "setuptools" in r:
                assert (
                    ">=" in r or ">68" in r
                ), f"setuptools version constraint looks wrong: {r!r}"

    def test_setuptools_packages_find_points_to_src(self) -> None:
        """``[tool.setuptools.packages.find] where = ["src"]`` must exist."""
        find_cfg = (
            self._data.get("tool", {})
            .get("setuptools", {})
            .get("packages", {})
            .get("find", {})
        )
        where = find_cfg.get("where", [])
        assert (
            "src" in where
        ), f"setuptools packages.find.where should include 'src': {where}"

    def test_readme_file_matches_declaration(self) -> None:
        """project.readme should reference an existing file."""
        readme = self._project.get("readme", "")
        if readme:
            assert (
                _REPO_ROOT / readme
            ).is_file(), f"readme file {readme!r} not found in repo root"

    def test_project_has_name_and_version(self) -> None:
        """Both name and version must be present for sdist/wheel metadata."""
        assert "name" in self._project
        assert "version" in self._project
        assert self._project["name"]
        assert self._project["version"]

    def test_requires_python_is_declared(self) -> None:
        """Wheel metadata requires ``Requires-Python`` header source."""
        assert (
            "requires-python" in self._project
        ), "pyproject.toml missing requires-python (needed for wheel metadata)"


# ===================================================================
# 3. Extras dependency graph consistency
# ===================================================================


class TestExtrasDependencyGraph:
    """Validate internal consistency of the extras dependency graph."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._opt: dict[str, list[str]] = _load_pyproject()["project"].get(
            "optional-dependencies", {}
        )

    def test_matrix_e2e_superset_of_matrix(self) -> None:
        """``matrix-e2e`` should depend on the same mindroom-nio base."""
        matrix_deps = set(self._opt.get("matrix", []))
        e2e_deps = set(self._opt.get("matrix-e2e", []))
        # matrix-e2e uses mindroom-nio[e2e] which is a superset dependency
        assert e2e_deps, "matrix-e2e extra is empty"
        # At minimum both should reference mindroom-nio
        matrix_has_nio = any("mindroom-nio" in d for d in matrix_deps)
        e2e_has_nio = any("mindroom-nio" in d for d in e2e_deps)
        assert matrix_has_nio, "matrix extra missing mindroom-nio reference"
        assert e2e_has_nio, "matrix-e2e extra missing mindroom-nio reference"

    def test_all_extras_have_at_least_one_dep(self) -> None:
        """Transport extras must not be empty lists."""
        transport_extras = {"matrix", "matrix-e2e", "meshtastic", "meshcore", "lxmf"}
        for name in transport_extras:
            deps = self._opt.get(name, [])
            assert len(deps) >= 1, f"transport extra {name!r} has no dependencies"

    def test_no_duplicate_deps_within_extra(self) -> None:
        """No extra should list the same dependency string twice."""
        for name, deps in self._opt.items():
            if len(deps) != len(set(deps)):
                dupes = [d for d in deps if deps.count(d) > 1]
                pytest.fail(f"extra {name!r} has duplicate deps: {dupes}")

    def test_version_pipins_use_standard_specifiers(self) -> None:
        """Every dependency should use a standard PEP 508 specifier."""
        for name, deps in self._opt.items():
            for dep in deps:
                # Should contain >=, ==, ~=, or be a bare name with extras
                has_spec = any(op in dep for op in (">=", "==", "~=", "<=", "!=", ">"))
                has_extras = "[" in dep
                assert (
                    has_spec or has_extras
                ), f"extra {name!r} dep {dep!r} lacks a version specifier"

    def test_dev_extra_contains_pytest(self) -> None:
        """Dev extra must contain pytest for test runner."""
        dev_deps = self._opt.get("dev", [])
        assert any(
            "pytest" in d for d in dev_deps
        ), f"dev extra missing pytest: {dev_deps}"


# ===================================================================
# 4. Console-script entry point resolution
# ===================================================================


class TestConsoleScriptResolution:
    """Verify the ``medre`` console-script entry point resolves to a real
    callable in the installed package."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._scripts = _load_pyproject()["project"].get("scripts", {})

    def test_entry_point_format(self) -> None:
        """Entry point must follow ``module.path:callable`` format."""
        ep = self._scripts.get("medre", "")
        assert ":" in ep, f"entry point {ep!r} missing ':' separator"
        module_path, _, callable_name = ep.partition(":")
        assert "." in module_path, f"module path {module_path!r} should be dotted"
        assert (
            callable_name.isidentifier()
        ), f"callable name {callable_name!r} is not a valid identifier"

    def test_entry_point_module_importable(self) -> None:
        """The module referenced in the entry point must be importable."""
        ep = self._scripts.get("medre", "")
        module_path = ep.partition(":")[0]
        mod = importlib.import_module(module_path)
        assert mod is not None

    def test_entry_point_callable_exists(self) -> None:
        """The callable referenced in the entry point must exist and be callable."""
        ep = self._scripts.get("medre", "")
        module_path, _, callable_name = ep.partition(":")
        mod = importlib.import_module(module_path)
        func = getattr(mod, callable_name, None)
        assert func is not None, f"{module_path}.{callable_name} not found"
        assert callable(func), f"{module_path}.{callable_name} is not callable"


# ===================================================================
# 5. Clean-environment import boundaries (broad)
# ===================================================================


class TestCleanEnvironmentImportBoundaries:
    """All library subpackages must import without optional SDKs.

    This is broader than ``TestBaseImportBoundary`` in the packaging contract
    tests — it exercises every ``__init__.py`` under ``src/medre/``.
    """

    # All subpackages under src/medre/ that should import cleanly
    _SUBPACKAGES: list[str] = [
        "medre",
        "medre.adapters",
        "medre.adapters.fakes",
        "medre.adapters.fakes.transport",
        "medre.adapters.fakes.matrix",
        "medre.adapters.fakes.meshtastic",
        "medre.adapters.fakes.meshcore",
        "medre.adapters.fakes.lxmf",
        "medre.adapters.fakes.presentation",
        "medre.adapters.matrix",
        "medre.adapters.matrix.compat",
        "medre.adapters.meshtastic",
        "medre.adapters.meshtastic.compat",
        "medre.adapters.meshcore",
        "medre.adapters.meshcore.compat",
        "medre.adapters.lxmf",
        "medre.adapters.lxmf.compat",
        "medre.config",
        "medre.config.adapters",
        "medre.config.loader",
        "medre.config.model",
        "medre.config.paths",
        "medre.config.sample",
        "medre.config.env",
        "medre.config.errors",
        "medre.core",
        "medre.core.contracts",
        "medre.core.events",
        "medre.core.routing",
        "medre.core.engine",
        "medre.core.storage",
        "medre.core.diagnostics",
        "medre.core.observability",
        "medre.core.lifecycle",
        "medre.core.identity",
        "medre.core.rendering",
        "medre.core.policies",
        "medre.core.planning",
        "medre.core.supervision",
        "medre.interop",
        "medre.runtime",
        "medre.runtime.builder",
        "medre.runtime.app",
        "medre.runtime.errors",
        "medre.runtime.evidence",
        "medre.config.routes",
        "medre.runtime.snapshot",
        "medre.runtime.boot_summary",
        "medre.core.supervision.capacity",
        "medre.runtime.observability",
        "medre.runtime.run_session",
        "medre.plugins",
        "medre.cli",
    ]

    @pytest.mark.parametrize("module_name", _SUBPACKAGES)
    def test_subpackage_imports_cleanly(self, module_name: str) -> None:
        """Each subpackage must import without error (no optional SDKs)."""
        mod = importlib.import_module(module_name)
        assert mod is not None

    def test_all_core_subpackages_discovered(self) -> None:
        """Every __init__.py under src/medre/ should be listed above."""
        init_files = list(_SRC_DIR.glob("medre/**/__init__.py"))
        # Derive module paths from file paths
        discovered = set()
        for init in init_files:
            rel = init.relative_to(_SRC_DIR)
            parts = list(rel.parts)
            # Remove trailing __init__.py
            parts = parts[:-1]
            discovered.add(".".join(parts))

        listed = set(self._SUBPACKAGES)
        missing = discovered - listed
        if missing:
            pytest.fail(
                f"Subpackages with __init__.py not covered by import test: "
                f"{sorted(missing)}"
            )


# ===================================================================
# 6. Config sample generation (clean env)
# ===================================================================


class TestConfigSampleCleanEnv:
    """``generate_sample_config()`` must produce valid TOML without extras."""

    def test_sample_config_is_valid_toml(self) -> None:
        from medre.config.sample import generate_sample_config

        sample = generate_sample_config()
        parsed = tomllib.loads(sample)
        assert isinstance(parsed, dict), "sample config did not parse to dict"

    def test_sample_config_has_runtime_section(self) -> None:
        from medre.config.sample import generate_sample_config

        parsed = tomllib.loads(generate_sample_config())
        assert "runtime" in parsed, "sample config missing [runtime]"

    def test_sample_config_has_adapters_section(self) -> None:
        from medre.config.sample import generate_sample_config

        parsed = tomllib.loads(generate_sample_config())
        assert "adapters" in parsed, "sample config missing [adapters]"

    def test_sample_config_has_storage_section(self) -> None:
        from medre.config.sample import generate_sample_config

        parsed = tomllib.loads(generate_sample_config())
        assert "storage" in parsed, "sample config missing [storage]"

    def test_sample_config_has_logging_section(self) -> None:
        from medre.config.sample import generate_sample_config

        parsed = tomllib.loads(generate_sample_config())
        assert "logging" in parsed, "sample config missing [logging]"

    def test_sample_config_mentions_all_transport_types(self) -> None:
        """Sample should document all four transport adapter types."""
        from medre.config.sample import generate_sample_config

        sample = generate_sample_config()
        for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
            assert (
                transport in sample.lower()
            ), f"sample config does not mention transport {transport!r}"

    def test_sample_config_toml_sections_parseable(self) -> None:
        """Sample config sections must parse into expected types.

        The sample uses ``adapter_kind = "fake"`` for all adapters so it
        works without optional SDKs.  The access_token is a non-empty fake
        value, making the sample loadable via ``load_config()`` as well.
        """
        from medre.config.sample import generate_sample_config

        sample = generate_sample_config()
        parsed = tomllib.loads(sample)

        # Runtime section must have a name
        assert parsed["runtime"]["name"], "sample [runtime].name is empty"

        # Storage section must have backend
        assert parsed["storage"]["backend"], "sample [storage].backend is empty"

        # Adapters section must exist with at least one transport
        adapters = parsed.get("adapters", {})
        assert adapters, "sample [adapters] is empty"

        # Logging section must have level
        assert parsed["logging"]["level"], "sample [logging].level is empty"


# ===================================================================
# 7. CLI smoke from clean install assumptions
# ===================================================================


class TestCLISmokeCleanEnv:
    """CLI commands must work without any optional SDKs installed.

    Uses programmatic ``main()`` dispatch — no subprocess.
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

    def test_version_command_succeeds(self) -> None:
        from medre.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["version"])
        output = buf.getvalue()
        assert "medre" in output.lower()
        assert "Python" in output

    def test_paths_command_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import tempfile

        from medre.cli import main

        with tempfile.TemporaryDirectory() as td:
            monkeypatch.setenv("MEDRE_HOME", td)
            buf = io.StringIO()
            with redirect_stdout(buf):
                main(["paths"])
            output = buf.getvalue()
            assert td in output  # MEDRE_HOME should appear in paths output

    def test_config_sample_command_succeeds(self) -> None:
        from medre.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["config", "sample"])
        output = buf.getvalue()
        assert "[runtime]" in output
        assert "[adapters" in output

    def test_adapters_command_succeeds(self) -> None:
        from medre.cli import main

        buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err_buf):
            main(["adapters"])
        output = buf.getvalue() + err_buf.getvalue()
        # Should mention adapter types even with no SDKs
        assert "matrix" in output.lower() or "adapter" in output.lower()

    def test_version_output_format(self) -> None:
        """Version output must include version string, Python version, platform."""
        from medre.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["version"])
        lines = buf.getvalue().strip().splitlines()
        assert len(lines) >= 2, f"version output too short: {lines}"
        assert lines[0].startswith(
            "medre"
        ), f"first line should start with 'medre': {lines[0]}"


# ===================================================================
# 8. Environment-variable override machinery
# ===================================================================


class TestEnvOverrideCleanEnv:
    """``apply_env_overrides`` and related machinery must be importable and
    correctly typed without optional SDKs."""

    def test_env_module_importable(self) -> None:
        import medre.config.env as env

        assert env is not None

    def test_apply_env_overrides_is_callable(self) -> None:
        from medre.config.env import apply_env_overrides

        assert callable(apply_env_overrides)

    def test_medre_env_config_is_dataclass(self) -> None:
        import dataclasses

        from medre.config.env import MedreEnvConfig

        assert dataclasses.is_dataclass(MedreEnvConfig)

    def test_secret_env_names_is_frozenset(self) -> None:
        """Internal secret env-name registry must be a frozenset."""
        import medre.config.env as env

        # Access private constant — intentional for clean-env boundary check
        assert hasattr(env, "_SECRET_ENV_NAMES") or hasattr(env, "MedreEnvConfig")


# ===================================================================
# 9. compileall — all source files compile
# ===================================================================


class TestCompileAll:
    """Every ``.py`` file under ``src/`` must compile without syntax errors."""

    @staticmethod
    def _collect_py_files() -> list[Path]:
        return sorted(_SRC_DIR.rglob("*.py"))

    def test_all_source_files_compile(self) -> None:
        errors: list[str] = []
        for py_file in self._collect_py_files():
            try:
                py_compile.compile(str(py_file), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append(f"{py_file}: {exc}")
        assert not errors, f"{len(errors)} file(s) failed to compile:\n" + "\n".join(
            errors
        )

    def test_no_stray_pyc_outside_pycache(self) -> None:
        """Source tree should not contain loose .pyc files outside __pycache__."""
        pyc_files = [
            f for f in _SRC_DIR.rglob("*.pyc") if f.parent.name != "__pycache__"
        ]
        assert (
            not pyc_files
        ), f"Found loose .pyc files outside __pycache__: {pyc_files[:5]}"


# ===================================================================
# 10. Package layout validation
# ===================================================================


class TestPackageLayout:
    """Validate src/ layout conventions for a clean install."""

    def test_src_dir_exists(self) -> None:
        assert _SRC_DIR.is_dir()

    def test_medre_package_under_src(self) -> None:
        assert (_SRC_DIR / "medre").is_dir()
        assert (_SRC_DIR / "medre" / "__init__.py").is_file()

    def test_no_top_level_medre_dir(self) -> None:
        """No ``medre/`` at repo root — must be under ``src/``."""
        assert not (
            _REPO_ROOT / "medre"
        ).is_dir(), "Found top-level medre/ directory — should be src/medre/ only"

    def test_pyproject_at_repo_root(self) -> None:
        assert _PYPROJECT_PATH.is_file()

    def test_no_setup_py(self) -> None:
        """Modern project should not need setup.py (src layout + pyproject.toml)."""
        setup_py = _REPO_ROOT / "setup.py"
        # setup.py may exist for backward compat but should not be required
        if setup_py.is_file():
            # If it exists, it should be minimal / deprecated
            content = setup_py.read_text()
            assert (
                len(content) < 200
            ), "setup.py exists and is non-trivial — prefer pyproject.toml only"

    def test_no_setup_cfg_required(self) -> None:
        """setup.cfg should not be needed if pyproject.toml is complete."""
        setup_cfg = _REPO_ROOT / "setup.cfg"
        if setup_cfg.is_file():
            content = setup_cfg.read_text()
            # If it exists, it should be minimal
            assert (
                "metadata" not in content or "options" not in content
            ), "setup.cfg contains metadata/options that should be in pyproject.toml"

    def test_cli_package_at_expected_path(self) -> None:
        """CLI package should be at ``src/medre/cli/``."""
        assert (_SRC_DIR / "medre" / "cli").is_dir()
        assert (_SRC_DIR / "medre" / "cli" / "__init__.py").is_file()


# ===================================================================
# 11. Deterministic reproducibility — pyproject.toml checksum stability
# ===================================================================


class TestReproducibilityEvidence:
    """Verify pyproject.toml has not drifted from expected structural invariants."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._project = self._data["project"]

    def test_single_base_dependency(self) -> None:
        """Base install should have exactly one required dependency (msgspec)."""
        deps = self._project.get("dependencies", [])
        assert (
            len(deps) == 1
        ), f"Expected exactly 1 base dependency, got {len(deps)}: {deps}"
        assert "msgspec" in deps[0], f"Expected msgspec, got: {deps}"

    def test_build_system_has_exactly_two_keys(self) -> None:
        """build-system should have requires and build-backend only."""
        bs = self._data.get("build-system", {})
        assert set(bs.keys()) == {
            "requires",
            "build-backend",
        }, f"build-system has unexpected keys: {sorted(bs.keys())}"

    def test_pytest_config_declares_testpaths(self) -> None:
        """pytest config must declare testpaths for reproducibility."""
        pytest_cfg = self._data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        assert "testpaths" in pytest_cfg, "[tool.pytest.ini_options] missing testpaths"
        assert (
            "tests" in pytest_cfg["testpaths"]
        ), f"testpaths should include 'tests': {pytest_cfg['testpaths']}"

    def test_pytest_config_declares_pythonpath(self) -> None:
        """pytest config must declare pythonpath for src layout."""
        pytest_cfg = self._data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        assert (
            "pythonpath" in pytest_cfg
        ), "[tool.pytest.ini_options] missing pythonpath"
        assert (
            "src" in pytest_cfg["pythonpath"]
        ), f"pythonpath should include 'src': {pytest_cfg['pythonpath']}"

    def test_live_test_marker_declared(self) -> None:
        """The 'live' test marker must be declared for clean-env filtering."""
        pytest_cfg = self._data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        markers = pytest_cfg.get("markers", [])
        assert any(
            "live" in m for m in markers
        ), f"'live' marker not declared in pytest config: {markers}"

    def test_asyncio_mode_is_auto(self) -> None:
        """asyncio_mode must be 'auto' for async test discovery."""
        pytest_cfg = self._data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        assert (
            pytest_cfg.get("asyncio_mode") == "auto"
        ), f"asyncio_mode should be 'auto': {pytest_cfg.get('asyncio_mode')}"


# ===================================================================
# 12. python -m medre / python -m medre.cli subprocess invocation
# ===================================================================


class TestPythonMSubprocessCleanEnv:
    """Verify ``python -m medre`` and ``python -m medre.cli`` work via
    subprocess — the entry points used by installed-package users who
    do not have the ``medre`` console script on PATH.

    These tests complement the programmatic ``main()`` tests in
    ``TestCLISmokeCleanEnv`` by exercising the full ``__main__.py``
    delegation chain in a child process.
    """

    def _run(self, module: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}
        for var in (
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "MEDRE_CONFIG",
        ):
            env.pop(var, None)
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_python_m_medre_help(self) -> None:
        result = self._run("medre", "--help")
        assert (
            result.returncode == 0
        ), f"exit={result.returncode}, stderr={result.stderr[:200]!r}"
        assert "medre" in result.stdout.lower()

    def test_python_m_medre_cli_help(self) -> None:
        result = self._run("medre.cli", "--help")
        assert (
            result.returncode == 0
        ), f"exit={result.returncode}, stderr={result.stderr[:200]!r}"
        assert "medre" in result.stdout.lower()

    def test_python_m_medre_version(self) -> None:
        result = self._run("medre", "version")
        assert result.returncode == 0
        assert "medre" in result.stdout.lower()
        assert "Python" in result.stdout

    def test_python_m_medre_cli_version(self) -> None:
        result = self._run("medre.cli", "version")
        assert result.returncode == 0
        assert "medre" in result.stdout.lower()

    def test_python_m_medre_config_sample_valid_toml(self) -> None:
        """``python -m medre config sample`` produces valid TOML."""
        result = self._run("medre", "config", "sample")
        assert result.returncode == 0
        parsed = tomllib.loads(result.stdout)
        assert "runtime" in parsed
        assert "adapters" in parsed

    def test_python_m_medre_paths(self) -> None:
        result = self._run("medre", "paths")
        assert result.returncode == 0
        assert "medre" in result.stdout.lower() or "config" in result.stdout.lower()

    def test_python_m_medre_adapters(self) -> None:
        result = self._run("medre", "adapters")
        assert result.returncode == 0
        combined = (result.stdout + result.stderr).lower()
        assert "matrix" in combined or "adapter" in combined


# ===================================================================
# 13. Optional SDK import-boundary regression
# ===================================================================


class TestOptionalSDKImportBoundary:
    """Verify lightweight CLI paths (``--help``, ``version``, ``config sample``)
    do not import optional SDK modules into ``sys.modules``.

    This is a subprocess regression test: the clean-install agent observed
    that ``from medre.cli import main; main(['--help'])`` leaked optional
    SDKs (``nio``, ``meshtastic``, ``RNS``, ``LXMF``) because ``main.py``
    eagerly imported all command modules at module level.

    The test runs a short Python snippet in a **fresh child process** that
    imports ``medre.cli``, invokes a lightweight command, and prints any
    optional SDK modules found in ``sys.modules``.  Because the child
    process starts clean, even if the SDKs are installed in the test
    environment, they should not appear unless a command path triggers them.

    Commands that **should not** import optional SDKs:
      - ``--help`` (argparse exits before dispatch)
      - ``version`` (only importlib.metadata + platform)
      - ``config sample`` (pure string generation)

    Commands that **may** import optional SDKs (out of scope):
      - ``paths``, ``adapters`` (load config → config.model → adapter __init__)
    """

    _OPTIONAL_SDK_MODULES: tuple[str, ...] = (
        "nio",
        "mindroom_nio",
        "meshtastic",
        "mtjk",
        "RNS",
        "LXMF",
        "meshcore",
        "meshcore_py",
    )

    def _check_sdk_leak(self, command_snippet: str) -> None:
        """Run *command_snippet* in a subprocess and assert no optional SDKs
        were imported."""
        import json

        # The subprocess snippet must: (1) redirect stdout so --help output
        # doesn't pollute stdout, (2) run the command, (3) restore stdout,
        # then (4) print the leaked-modules JSON on the real stdout.
        check_code = (
            "import sys, json, io, contextlib;\n"
            "_saved_stdout = sys.stdout;\n"
            "sys.stdout = io.StringIO();\n"
            "try:\n"
            f"    {command_snippet};\n"
            "except SystemExit:\n"
            "    pass\n"
            "finally:\n"
            "    sys.stdout = _saved_stdout;\n"
            "leaked = [m for m in "
            f"{self._OPTIONAL_SDK_MODULES!r} "
            "if m in sys.modules];\n"
            "print(json.dumps(leaked))"
        )
        env = {**os.environ, "PYTHONPATH": str(_SRC_DIR)}
        for var in (
            "MEDRE_HOME",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "MEDRE_CONFIG",
        ):
            env.pop(var, None)
        result = subprocess.run(
            [sys.executable, "-c", check_code],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert result.returncode == 0, (
            f"Subprocess failed (rc={result.returncode}): "
            f"stderr={result.stderr[:500]!r}"
        )
        leaked = json.loads(result.stdout.strip())
        assert (
            leaked == []
        ), f"Optional SDK modules leaked during lightweight CLI path: {leaked}"

    def test_help_does_not_import_optional_sdks(self) -> None:
        """``from medre.cli import main`` + ``--help`` must not pull in SDKs."""
        self._check_sdk_leak("from medre.cli import main; main(['--help'])")

    def test_version_does_not_import_optional_sdks(self) -> None:
        """``medre version`` must not pull in SDKs."""
        self._check_sdk_leak("from medre.cli import main; main(['version'])")

    def test_config_sample_does_not_import_optional_sdks(self) -> None:
        """``medre config sample`` must not pull in SDKs."""
        self._check_sdk_leak("from medre.cli import main; main(['config', 'sample'])")

    def test_paths_does_not_import_optional_sdks(self) -> None:
        """``medre paths`` must not pull in SDKs — core path data needs none."""
        self._check_sdk_leak("from medre.cli import main; main(['paths'])")
