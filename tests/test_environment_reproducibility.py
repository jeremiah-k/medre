"""Track 3/4: Environment reproducibility validation tests.

Validates that MEDRE produces a **deterministic, reproducible** environment
across installations, path resolution modes, and CLI invocations.  Every
test in this module:

- Runs **without** any optional transport SDK (no matrix, meshtastic,
  meshcore, or lxmf libraries installed).
- Exercises **path resolution, metadata, fake runtime lifecycle, env-var
  coverage, and CLI workflows** only — no live network, no hardware, no
  ``pip install`` / ``python -m build``.
- Produces **deterministic** pass/fail results.
- Does **not** duplicate ``test_clean_environment.py`` (which covers
  editable install docs, build metadata, extras graph, console scripts,
  subpackage imports, config sample, CLI smoke, env override, compileall,
  package layout) or ``test_deployment_paths.py`` (path layout, bind-mount,
  _ensure_dirs tree, disabled adapters, cross-mode, non-overlap, diagnostics).

Focus areas:

  1. XDG variable independence and default fallback.
  2. MEDRE_HOME precedence over XDG.
  3. Reproducible path resolution (identical inputs → identical outputs).
  4. Distribution metadata completeness for wheel/sdist.
  5. Fake-runtime lifecycle without optional extras.
  6. docker.env.example key coverage against env module.
  7. CLI config-check and paths workflows under different modes.
  8. Version consistency.

NOT EXECUTED: No real ``pip install``, ``python -m build``, or clean-host
execution is performed.  These tests verify the *application logic and
metadata* that would be validated during a reproducibility audit, not the
actual package build pipeline.
"""

from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import resolve
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"
_DOCKER_ENV = _REPO_ROOT / "examples" / "env" / "docker.env.example"
_SRC_DIR = _REPO_ROOT / "src"


def _load_pyproject() -> dict[str, Any]:
    with _PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Path-related env vars to clean
# ---------------------------------------------------------------------------

_PATH_ENV_VARS: tuple[str, ...] = (
    "MEDRE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no path-related env vars leak between tests."""
    for var in _PATH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runtime_with(
    *,
    matrix_ids: list[str] | None = None,
    meshtastic_ids: list[str] | None = None,
    storage_backend: str = "memory",
) -> RuntimeConfig:
    """Build a RuntimeConfig with specified adapters."""
    adapters = AdapterConfigSet()

    for aid in matrix_ids or []:
        adapters.matrix[aid] = MatrixRuntimeConfig(
            adapter_id=aid,
            enabled=True,
            adapter_kind="fake",
        )

    for aid in meshtastic_ids or []:
        adapters.meshtastic[aid] = MeshtasticRuntimeConfig(
            adapter_id=aid,
            enabled=True,
            adapter_kind="fake",
        )

    return RuntimeConfig(
        runtime=RuntimeOptions(name="env-repro-test"),
        logging=LoggingConfig(level="DEBUG"),
        storage=StorageConfig(backend=storage_backend),
        adapters=adapters,
    )


# ===================================================================
# 1. XDG variable independence
# ===================================================================


class TestXDGVariableIndependence:
    """Each XDG variable resolves independently of the others."""

    def test_xdg_config_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting only XDG_CONFIG_HOME affects config_dir."""
        cfg = tmp_path / "custom_config"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
        paths = resolve()
        assert paths.config_dir == cfg / "medre"
        # state_dir should use default (not affected by XDG_CONFIG_HOME)
        assert (
            "state" in str(paths.state_dir).lower() or paths.state_dir.name == "medre"
        )

    def test_xdg_state_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting only XDG_STATE_HOME affects state_dir, log_dir, database."""
        st = tmp_path / "custom_state"
        monkeypatch.setenv("XDG_STATE_HOME", str(st))
        paths = resolve()
        assert paths.state_dir == st / "medre"
        assert paths.log_dir == st / "medre" / "logs"
        assert paths.database_path == st / "medre" / "medre.sqlite"

    def test_xdg_data_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting only XDG_DATA_HOME affects data_dir."""
        data = tmp_path / "custom_data"
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        paths = resolve()
        assert paths.data_dir == data / "medre"

    def test_xdg_cache_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting only XDG_CACHE_HOME affects cache_dir."""
        cache = tmp_path / "custom_cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
        paths = resolve()
        assert paths.cache_dir == cache / "medre"

    def test_all_xdg_vars_independent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All four XDG vars can point to completely separate roots."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dat"))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
        paths = resolve()

        assert paths.config_dir == tmp_path / "cfg" / "medre"
        assert paths.state_dir == tmp_path / "st" / "medre"
        assert paths.data_dir == tmp_path / "dat" / "medre"
        assert paths.cache_dir == tmp_path / "cch" / "medre"
        # No overlap between these roots
        roots = {
            paths.config_dir,
            paths.state_dir,
            paths.data_dir,
            paths.cache_dir,
        }
        assert len(roots) == 4, "XDG paths should be fully independent"


# ===================================================================
# 2. XDG default fallback (no vars set)
# ===================================================================


class TestXDGDefaultFallback:
    """Without any XDG vars, resolve uses spec-defined defaults."""

    def test_config_dir_default(self) -> None:
        paths = resolve()
        assert paths.config_dir is not None
        # Default should be under ~/.config/medre
        assert paths.config_dir.name == "medre"

    def test_state_dir_default(self) -> None:
        paths = resolve()
        assert paths.state_dir.name == "medre"

    def test_database_under_state(self) -> None:
        paths = resolve()
        assert paths.database_path.parent == paths.state_dir

    def test_log_dir_under_state(self) -> None:
        """In XDG mode, log_dir is a child of state_dir."""
        paths = resolve()
        assert paths.log_dir.parent == paths.state_dir


# ===================================================================
# 3. MEDRE_HOME precedence over XDG
# ===================================================================


class TestMEDREHomePrecedence:
    """MEDRE_HOME takes absolute precedence over any XDG vars."""

    def test_medre_home_ignores_xdg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When MEDRE_HOME is set, all XDG vars are ignored."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        paths = resolve()

        # Everything should be under MEDRE_HOME
        assert str(paths.state_dir).startswith(str(tmp_path / "home"))
        assert str(paths.data_dir).startswith(str(tmp_path / "home"))
        assert paths.config_dir is None  # MEDRE_HOME mode has no config_dir

    def test_empty_medre_home_falls_through_to_xdg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An empty MEDRE_HOME is treated as unset."""
        monkeypatch.setenv("MEDRE_HOME", "")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        paths = resolve()

        # Should fall through to XDG mode
        assert paths.config_dir is not None
        assert str(paths.state_dir).startswith(str(tmp_path / "st"))

    def test_whitespace_medre_home_falls_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A whitespace-only MEDRE_HOME is treated as unset."""
        monkeypatch.setenv("MEDRE_HOME", "   ")
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        paths = resolve()

        assert paths.config_dir is not None


# ===================================================================
# 4. Reproducible path resolution
# ===================================================================


class TestReproduciblePathResolution:
    """resolve() called twice with same env gives identical results."""

    def test_medre_home_reproducible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two resolve() calls with same MEDRE_HOME produce identical paths."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "vol"))
        p1 = resolve()
        p2 = resolve()
        assert p1 == p2

    def test_xdg_reproducible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two resolve() calls with same XDG vars produce identical paths."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
        p1 = resolve()
        p2 = resolve()
        assert p1 == p2

    def test_default_reproducible(self) -> None:
        """Two resolve() calls with default env produce identical paths."""
        p1 = resolve()
        p2 = resolve()
        assert p1 == p2

    def test_frozen_paths_immutable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MedrePaths instances are frozen (immutable)."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        paths = resolve()
        with pytest.raises(AttributeError):
            paths.state_dir = tmp_path / "hacked"  # type: ignore[misc]


# ===================================================================
# 5. Distribution metadata completeness for wheel/sdist
# ===================================================================


class TestDistributionMetadataCompleteness:
    """All fields needed for wheel/sdist distribution artifacts exist."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._project: dict[str, Any] = self._data["project"]

    def test_classifiers_are_nonempty(self) -> None:
        """Classifiers list must exist and be non-empty."""
        classifiers = self._project.get("classifiers", [])
        assert len(classifiers) >= 1, "pyproject.toml missing classifiers"

    def test_has_development_status_classifier(self) -> None:
        """At least one Development Status classifier must be present."""
        classifiers = self._project.get("classifiers", [])
        dev_status = [c for c in classifiers if c.startswith("Development Status")]
        assert dev_status, "Missing 'Development Status ::' classifier"

    def test_has_python_version_classifiers(self) -> None:
        """Python version classifiers must be present."""
        classifiers = self._project.get("classifiers", [])
        py_classifiers = [
            c
            for c in classifiers
            if "Programming Language :: Python ::" in c and "3." in c
        ]
        assert py_classifiers, "Missing Python version classifiers"

    def test_license_field_present(self) -> None:
        """License field must be declared for wheel metadata."""
        assert "license" in self._project, "Missing [project].license"

    def test_description_present(self) -> None:
        """Description must be non-empty for package index."""
        desc = self._project.get("description", "")
        assert desc, "project.description is empty"

    def test_readme_referenced(self) -> None:
        """Readme must be referenced for long description."""
        readme = self._project.get("readme", "")
        assert readme, "project.readme not set"
        assert (_REPO_ROOT / readme).is_file(), f"readme file {readme!r} not found"

    def test_requires_python_compatible_with_classifiers(self) -> None:
        """requires-python must be consistent with Python classifiers."""
        rp = self._project.get("requires-python", "")
        classifiers = self._project.get("classifiers", [])
        # Extract minimum version from requires-python (e.g., ">=3.11" → "3.11")
        import re

        m = re.search(r">=\s*(\d+\.\d+)", rp)
        if m:
            min_ver = m.group(1)
            # At least one classifier should mention this version
            assert any(
                min_ver in c for c in classifiers
            ), f"requires-python >= {min_ver} but no classifier mentions {min_ver}"

    def test_typing_classifier_if_typed(self) -> None:
        """If Typed classifier is present, it must be valid."""
        classifiers = self._project.get("classifiers", [])
        typed = [c for c in classifiers if "Typed" in c]
        if typed:
            assert (
                typed[0] == "Typing :: Typed"
            ), f"Unexpected typing classifier: {typed[0]!r}"


# ===================================================================
# 6. Fake-runtime lifecycle without optional extras
# ===================================================================


class TestFakeRuntimeLifecycle:
    """Build, start, and stop a runtime using fake adapters only.

    No optional transport SDKs are required.  Uses adapter_kind="fake"
    for all adapters.
    """

    @pytest.mark.asyncio
    async def test_build_and_start_and_stop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Complete lifecycle: build → start → stop with fake adapters."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(
            matrix_ids=["mx"],
            meshtastic_ids=["mt"],
        )
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        await app.start()
        from medre.runtime.app import RuntimeState

        assert app.state == RuntimeState.RUNNING

        await app.stop()

    @pytest.mark.asyncio
    async def test_runtime_state_transitions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runtime state transitions: INITIALIZED → STARTING → RUNNING → STOPPING → STOPPED."""
        from medre.runtime.app import RuntimeState

        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(matrix_ids=["mx"])
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert app.state == RuntimeState.INITIALIZED

        await app.start()
        assert app.state == RuntimeState.RUNNING

        await app.stop()
        assert app.state == RuntimeState.STOPPED

    @pytest.mark.asyncio
    async def test_runtime_with_memory_storage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Runtime with memory backend doesn't touch disk for storage."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(
            matrix_ids=["mx"],
            storage_backend="memory",
        )
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        await app.start()
        await app.stop()

    def test_build_produces_app_with_adapters(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Builder produces app with correct adapter count."""
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
        config = _make_runtime_with(
            matrix_ids=["mx1", "mx2"],
            meshtastic_ids=["mt1"],
        )
        paths = resolve()
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert len(app.adapters) == 3
        assert "mx1" in app.adapters
        assert "mx2" in app.adapters
        assert "mt1" in app.adapters


# ===================================================================
# 7. docker.env.example key coverage against env module
# ===================================================================


class TestDockerEnvKeyCoverage:
    """Every env var key in docker.env.example is recognized by the env module."""

    def test_all_env_keys_recognized(self) -> None:
        """docker.env.example keys must be in ALL_RECOGNIZED_ENV_NAMES."""
        from medre.config.env import ALL_RECOGNIZED_ENV_NAMES

        assert _DOCKER_ENV.is_file()
        text = _DOCKER_ENV.read_text()
        # Extract variable names (lines like MEDRE_FOO=bar or # MEDRE_FOO=bar)
        env_keys: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            # Remove leading comment
            stripped = stripped.lstrip("#").strip()
            # Check for KEY= pattern
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key.startswith("MEDRE_") and key.isupper():
                    env_keys.add(key)

        # All extracted keys should be recognized — either as fixed core names,
        # as dynamically-prefixed MEDRE_ADAPTER__<TOKEN>__<FIELD> vars, or as
        # legacy transport-specific vars (MEDRE_MATRIX_*, MEDRE_MESHTASTIC_*,
        # etc.) which are documented for migration reference.
        _REJECTED_LEGACY_PREFIXES = (
            "MEDRE_MATRIX_",
            "MEDRE_MESHTASTIC_",
            "MEDRE_MESHCORE_",
            "MEDRE_LXMF_",
        )
        unrecognized = env_keys - ALL_RECOGNIZED_ENV_NAMES
        unrecognized = {
            k for k in unrecognized if not k.startswith("MEDRE_ADAPTER__")
        }
        unrecognized = {
            k
            for k in unrecognized
            if not any(k.startswith(p) for p in _REJECTED_LEGACY_PREFIXES)
        }
        assert (
            not unrecognized
        ), f"docker.env.example has unrecognized MEDRE_ keys: {sorted(unrecognized)}"

    def test_core_env_names_present(self) -> None:
        """Core env names are documented in docker.env.example."""

        text = _DOCKER_ENV.read_text()
        # MEDRE_HOME and MEDRE_LOG_LEVEL should be present
        assert "MEDRE_HOME" in text
        assert "MEDRE_LOG_LEVEL" in text


# ===================================================================
# 8. CLI config-check and paths workflows under different modes
# ===================================================================


class TestCLIConfigCheckWorkflow:
    """CLI config check command works with valid configs."""

    @pytest.fixture(autouse=True)
    def _clean_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in _PATH_ENV_VARS + ("MEDRE_CONFIG",):
            monkeypatch.delenv(var, raising=False)

    def test_config_check_with_explicit_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """config check --config <path> with a valid config succeeds."""
        from medre.cli import main

        # Write a minimal valid config
        config_file = tmp_path / "test.toml"
        config_file.write_text(
            '[runtime]\nname = "test"\n'
            '[logging]\nlevel = "INFO"\n'
            '[storage]\nbackend = "memory"\n'
            "[adapters]\n"
        )

        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            main(["config", "check", "--config", str(config_file)])

        output = buf.getvalue()
        assert "Config file:" in output or "error" not in err.getvalue().lower()


class TestCLIPathsUnderModes:
    """CLI paths command shows correct mode information."""

    @pytest.fixture(autouse=True)
    def _clean_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in _PATH_ENV_VARS + ("MEDRE_CONFIG",):
            monkeypatch.delenv(var, raising=False)

    def test_paths_shows_medre_home_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """paths command shows MEDRE_HOME mode when MEDRE_HOME is set."""
        from medre.cli import main

        home = tmp_path / "home"
        monkeypatch.setenv("MEDRE_HOME", str(home))

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["paths"])

        output = buf.getvalue()
        assert "MEDRE_HOME" in output

    def test_paths_shows_xdg_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """paths command shows XDG mode when MEDRE_HOME is not set."""
        from medre.cli import main

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["paths"])

        output = buf.getvalue()
        assert "XDG" in output


# ===================================================================
# 9. Version consistency
# ===================================================================


class TestVersionConsistency:
    """Version information is consistent across sources."""

    @pytest.fixture(autouse=True)
    def _load(self) -> None:
        self._data = _load_pyproject()
        self._project = self._data["project"]

    def test_get_version_fallback_matches_pyproject(self) -> None:
        """_get_version() fallback matches pyproject.toml version.

        When the package is not installed (e.g. editable install without
        build), _get_version falls back to the hardcoded value.  That value
        should match pyproject.toml.
        """
        from medre.cli.main import _get_version

        pyproject_version = self._project["version"]
        cli_version = _get_version()
        # In an editable install, importlib.metadata may return the installed
        # version which should also match pyproject.toml
        assert (
            cli_version == pyproject_version
        ), f"CLI version {cli_version!r} != pyproject version {pyproject_version!r}"

    def test_version_is_pep440_compatible(self) -> None:
        """Version string should be PEP 440 compatible (basic check)."""
        version = self._project["version"]
        parts = version.split(".")
        assert len(parts) >= 2, f"Version {version!r} should have ≥2 parts"
        for part in parts:
            assert part.isdigit(), f"Version part {part!r} is not numeric"

    def test_version_command_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """version command outputs consistent version."""
        from medre.cli import main

        for var in _PATH_ENV_VARS:
            monkeypatch.delenv(var, raising=False)

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["version"])

        output = buf.getvalue()
        version = self._project["version"]
        assert (
            version in output
        ), f"Version {version!r} not found in 'medre version' output"


# ===================================================================
# 10. Environment reproducibility between resolution modes
# ===================================================================


class TestCrossModeReproducibility:
    """Switching between modes produces predictable results."""

    def test_switching_medre_home_to_xdg_produces_different_paths(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MEDRE_HOME and XDG modes produce distinct path sets."""
        # MEDRE_HOME mode
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        paths_home = resolve()

        # XDG mode
        monkeypatch.delenv("MEDRE_HOME", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_st"))
        paths_xdg = resolve()

        assert paths_home.state_dir != paths_xdg.state_dir
        assert paths_home.config_dir is None
        assert paths_xdg.config_dir is not None

    def test_switching_xdg_to_medre_home_uses_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Setting MEDRE_HOME after XDG overrides XDG paths."""
        # XDG mode first
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg_st"))
        paths_xdg = resolve()
        xdg_state = paths_xdg.state_dir

        # Now set MEDRE_HOME
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
        paths_home = resolve()

        assert paths_home.state_dir != xdg_state
        assert paths_home.state_dir == tmp_path / "home" / "state"
