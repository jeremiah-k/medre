"""Deployment / reproducibility boundary enforcement tests.

These tests enforce structural invariants that guarantee reproducible
deployments and clean-environment test runs.  They use **source-level text
inspection** (not runtime importing of optional SDKs) and cover:

1. Clean-env test files do not require live SDKs.
2. Config subsystem modules do not import transport SDKs.
3. Deployment helpers do not instantiate SDKs.
4. CLI remains transport-agnostic.
5. Soak framework files remain fake-only unless explicitly live-marked.
6. No live tests run by default — ``addopts`` and marker discipline.
7. Snapshot module has no transport coupling.

Pattern
-------
All tests use source-level text inspection.  This avoids triggering SDK
imports at test collection time and works in environments where some or
all SDKs are not installed.

Adapter config dataclasses (``medre.config.adapters.*``) are pure frozen
dataclasses with no SDK dependency.  Imports of these modules are **not**
flagged as violations — only runtime modules (adapter, session, codec)
and direct SDK imports are banned.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.architecture_report import _BANNED_SDK_IMPORT_PREFIXES, _SDK_PACKAGES
from tests.helpers.sdk_constants import _SDK_INSTANTIATION_PATTERNS
from tests.helpers.source_reader import source_of as _source_of

# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_architectural_boundaries.py)
# ---------------------------------------------------------------------------

_ADAPTER_PREFIXES = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)
"""Concrete adapter package prefixes (excludes medre.core.contracts.adapter and fake_*)."""

_ADAPTER_COMPAT_MODULES = (
    "medre.adapters.matrix.compat",
    "medre.adapters.meshtastic.compat",
    "medre.adapters.meshcore.compat",
    "medre.adapters.lxmf.compat",
)
"""Adapter compat modules that are ALLOWED to import SDKs internally."""

_TESTS_DIR = Path(__file__).parent
"""Root tests directory."""

_REPO_ROOT = _TESTS_DIR.parent
"""Repository root directory."""


# Adapter runtime module imports banned in deployment/clean-env contexts.
# Config imports (medre.config.adapters.*) are pure dataclasses — permitted.
_BANNED_ADAPTER_RUNTIME_IMPORTS = (
    "from medre.adapters.matrix.adapter",
    "from medre.adapters.matrix.session",
    "from medre.adapters.matrix.codec",
    "from medre.adapters.meshtastic.adapter",
    "from medre.adapters.meshtastic.session",
    "from medre.adapters.meshtastic.codec",
    "from medre.adapters.meshtastic.queue",
    "from medre.adapters.meshcore.adapter",
    "from medre.adapters.meshcore.session",
    "from medre.adapters.meshcore.codec",
    "from medre.adapters.lxmf.adapter",
    "from medre.adapters.lxmf.session",
    "from medre.adapters.lxmf.codec",
)


def _import_lines(source: str) -> list[str]:
    """Extract all import/from-import lines from source text.

    See also: architecture_ast.runtime_scope_imports() for AST-based
    import extraction (returns ImportRecord objects with resolved names).
    """
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def _banned_imports(lines: list[str], banned: tuple[str, ...]) -> list[str]:
    """Return import lines referencing any banned package.

    See also: architecture_ast.import_matches() for module-prefix matching
    on resolved module names (AST-level, not text-level).
    """
    found: list[str] = []
    for line in lines:
        for b in banned:
            if re.search(rf"\b{re.escape(b)}\b", line):
                found.append(line)
                break
    return found


def _file_source(path: Path) -> str:
    """Read source from a file path."""
    return path.read_text()


def _scan_file_for_banned_imports(
    path: Path,
    banned: tuple[str, ...],
) -> list[str]:
    """Scan a test file for banned import-line prefixes.

    Returns list of ``"{filename}:{lineno}: {line}"`` strings for violations.
    Skips comment lines.
    """
    source = _file_source(path)
    violations: list[str] = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pattern in banned:
            if pattern in stripped:
                violations.append(f"{path.name}:{i}: {stripped}")
                break
    return violations


def _has_live_marker(path: Path) -> bool:
    """Return True if the test file declares a live marker.

    Checks for:
    - ``pytestmark = pytest.mark.live``
    - ``pytestmark = [pytest.mark.live]``
    - ``@pytest.mark.live`` decorator
    """
    source = _file_source(path)
    return bool(re.search(r"pytest\.mark\.live", source))


def _has_hardware_marker(path: Path) -> bool:
    """Return True if the test file declares a hardware marker.

    Checks for:
    - ``pytestmark = pytest.mark.hardware``
    - ``pytestmark = [pytest.mark.hardware]``
    - ``@pytest.mark.hardware`` decorator
    """
    source = _file_source(path)
    return bool(re.search(r"pytest\.mark\.hardware", source))


# ===================================================================
# 1. Clean-env test files do not require live SDKs
# ===================================================================


class TestCleanEnvTestsNoLiveSdk:
    """Test files that must work in a bare ``pip install .`` environment.

    These files exercise packaging, configuration, deployment, and
    reproducibility contracts.  They must never import transport SDKs
    or concrete adapter runtime modules.
    """

    _CLEAN_ENV_TEST_FILES = [
        "test_clean_environment.py",
        "test_packaging_and_install_contract.py",
        "test_deployment_paths.py",
        "test_example_configs.py",
        "test_runtime_builder.py",
        "test_fake_runtime_smoke.py",
        "test_runtime_hygiene.py",
    ]

    @pytest.fixture(
        params=_CLEAN_ENV_TEST_FILES,
        ids=_CLEAN_ENV_TEST_FILES,
    )
    def clean_env_file(self, request: Any) -> Path:
        """Parametrized fixture for each clean-env test file."""
        path = _TESTS_DIR / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_clean_env_files_no_sdk_imports(
        self,
        clean_env_file: Path,
    ) -> None:
        """Clean-env test files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            clean_env_file,
            _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert (
            violations == []
        ), "Clean-env test file has transport SDK imports:\n" + "\n".join(violations)

    def test_clean_env_files_no_adapter_runtime_imports(
        self,
        clean_env_file: Path,
    ) -> None:
        """Clean-env test files must not import concrete adapter runtime modules.

        Config dataclass imports (``medre.config.adapters.*``) are
        permitted — they are pure data with no SDK dependency.
        """
        violations = _scan_file_for_banned_imports(
            clean_env_file,
            _BANNED_ADAPTER_RUNTIME_IMPORTS,
        )
        assert (
            violations == []
        ), "Clean-env test file has adapter runtime imports:\n" + "\n".join(violations)


# ===================================================================
# 2. Config subsystem modules do not import transport SDKs
# ===================================================================


class TestConfigSubsystemNoSdk:
    """Configuration subsystem modules must not import transport SDKs.

    The config layer (loader, paths, errors, model, env) is consumed by
    the CLI, runner, and builder in clean environments.  It must never
    depend on optional transport SDK packages.

    Note: ``medre.config.model`` and ``medre.config.env`` import adapter
    config dataclasses (``medre.config.adapters.*``).  These are pure
    frozen dataclasses with no SDK dependency and are excluded from the
    concrete adapter ban.
    """

    _CONFIG_MODULES = [
        "medre.config.loader",
        "medre.config.paths",
        "medre.config.errors",
        "medre.config.model",
        "medre.config.env",
        "medre.config.sample",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _CONFIG_MODULES,
    )
    def test_config_modules_no_sdk_imports(
        self,
        module_name: str,
    ) -> None:
        """Config modules must not have top-level SDK imports.

        Adapter config dataclass imports (``medre.config.adapters.*``)
        are excluded — they are pure frozen dataclasses with no SDK
        dependency.
        """
        source = _source_of(module_name)
        lines = _import_lines(source)

        # Exclude adapter config dataclass imports from the SDK check.
        # Lines like ``from medre.config.adapters.meshtastic import ...``
        # contain the word "meshtastic" but are safe pure-data imports.
        non_config_lines = [
            line
            for line in lines
            if not (line.startswith("from medre.adapters.") and ".config " in line)
            and not line.startswith("from medre.config.adapters.")
        ]
        banned = _banned_imports(non_config_lines, _SDK_PACKAGES)
        assert banned == [], f"{module_name} imports transport SDKs: {banned}"

    @pytest.mark.parametrize(
        "module_name",
        _CONFIG_MODULES,
    )
    def test_config_modules_no_sdk_instantiation(
        self,
        module_name: str,
    ) -> None:
        """Config modules must not directly instantiate SDK objects."""
        source = _source_of(module_name)

        instantiation_patterns = _SDK_INSTANTIATION_PATTERNS
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in instantiation_patterns:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert (
            violations == []
        ), f"{module_name} directly instantiates transport SDKs:\n" + "\n".join(
            violations
        )


# ===================================================================
# 3. Deployment helpers do not instantiate SDKs
# ===================================================================


class TestDeploymentHelpersNoSdkInstantiation:
    """Deployment helper modules must not directly instantiate transport
    SDKs.

    These modules bootstrap the runtime from configuration.  They may
    reference adapters through abstract interfaces or the builder
    pattern, but must never call SDK constructors directly.
    """

    _DEPLOYMENT_SOURCE_MODULES = [
        "medre.cli.run_commands",
        "medre.config.sample",
    ]

    _SDK_INSTANTIATION_PATTERNS = _SDK_INSTANTIATION_PATTERNS

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_sdk_imports(
        self,
        module_name: str,
    ) -> None:
        """Deployment modules must not have top-level SDK imports."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"{module_name} imports transport SDKs: {banned}"

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_concrete_adapter_imports(
        self,
        module_name: str,
    ) -> None:
        """Deployment modules must not import concrete adapter packages.

        Config dataclass imports (``medre.config.adapters.*``) are
        excluded from this check — they carry no SDK dependency.
        """
        source = _source_of(module_name)
        lines = _import_lines(source)

        # Filter out config imports before checking.
        non_config_lines = [
            line
            for line in lines
            if ".config" not in line or not line.startswith("from medre.adapters.")
        ]
        banned = _banned_imports(non_config_lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"{module_name} imports concrete adapter packages: {banned}"

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_sdk_instantiation(
        self,
        module_name: str,
    ) -> None:
        """Deployment modules must not directly instantiate SDK objects."""
        source = _source_of(module_name)

        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in self._SDK_INSTANTIATION_PATTERNS:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert (
            violations == []
        ), f"{module_name} directly instantiates transport SDKs:\n" + "\n".join(
            violations
        )

    def test_run_commands_uses_builder_pattern(self) -> None:
        """``medre.cli.run_commands`` must build the runtime through
        ``RuntimeBuilder`` — never through direct adapter construction."""
        source = _source_of("medre.cli.run_commands")
        assert (
            "RuntimeBuilder" in source
        ), "medre.cli.run_commands must use RuntimeBuilder to construct the runtime"

    def test_config_sample_no_sdk_references(self) -> None:
        """``medre.config.sample`` must not reference SDK modules in code."""
        source = _source_of("medre.config.sample")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"medre.config.sample imports transport SDKs: {banned}"


# ===================================================================
# 4. CLI remains transport-agnostic
# ===================================================================


class TestCliTransportAgnostic:
    """CLI module must remain transport-agnostic.

    The CLI may use ``importlib.import_module`` to check SDK availability
    (dynamic probing), but must never ``import nio``, ``import meshtastic``,
    etc. at module top level.  It must not directly instantiate SDK objects.
    """

    def test_cli_no_top_level_sdk_imports(self) -> None:
        """cli.py must not have top-level transport SDK imports."""
        source = _source_of("medre.cli")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"cli.py has top-level transport SDK imports: {banned}"

    def test_cli_no_concrete_adapter_imports(self) -> None:
        """cli.py must not import concrete adapter packages."""
        source = _source_of("medre.cli")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], f"cli.py imports concrete adapter packages: {banned}"

    def test_cli_sdk_probe_is_dynamic_only(self) -> None:
        """SDK availability checks in CLI must use importlib.import_module."""
        source = _source_of("medre.cli.config_commands")

        assert (
            "importlib.import_module" in source
        ), "CLI should use importlib.import_module for SDK probing"

        direct_instantiation_patterns = (
            "nio.AsyncClient(",
            "meshtastic.SerialInterface(",
            "meshtastic.tcp_interface.TCPInterface(",
            "RNS.Transport(",
        )
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in direct_instantiation_patterns:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert (
            violations == []
        ), "cli.py directly instantiates transport SDKs:\n" + "\n".join(violations)

    def test_cli_test_files_no_sdk_imports(self) -> None:
        """CLI test files must not import transport SDKs."""
        cli_test_files = [
            "test_cli.py",
            "test_operator_workflows.py",
            "test_operator_failures.py",
        ]
        violations: list[str] = []
        for filename in cli_test_files:
            path = _TESTS_DIR / filename
            if not path.exists():
                continue
            file_violations = _scan_file_for_banned_imports(
                path,
                _BANNED_SDK_IMPORT_PREFIXES,
            )
            for v in file_violations:
                violations.append(f"{filename}: {v}")

        assert (
            violations == []
        ), "CLI test files have transport SDK imports:\n" + "\n".join(violations)

    def test_cli_test_files_no_adapter_runtime_imports(self) -> None:
        """CLI test files must not import concrete adapter runtime modules."""
        cli_test_files = [
            "test_cli.py",
            "test_operator_workflows.py",
            "test_operator_failures.py",
        ]
        violations: list[str] = []
        for filename in cli_test_files:
            path = _TESTS_DIR / filename
            if not path.exists():
                continue
            file_violations = _scan_file_for_banned_imports(
                path,
                _BANNED_ADAPTER_RUNTIME_IMPORTS,
            )
            for v in file_violations:
                violations.append(f"{filename}: {v}")

        assert (
            violations == []
        ), "CLI test files have adapter runtime imports:\n" + "\n".join(violations)


# ===================================================================
# 5. Soak framework remains fake-only unless live-marked
# ===================================================================


class TestSoakFrameworkFakeOnly:
    """Soak test files must only use fake adapters unless explicitly
    live-marked.

    Fake-only soak files are strictly scanned for transport SDK imports.
    Live-marked soak files (``test_soak.py``) must carry the
    ``pytest.mark.live`` marker.
    """

    _FAKE_ONLY_SOAK_FILES = [
        "test_soak_harness.py",
        "test_soak_config_builder.py",
        "test_soak_foundations_v2.py",
        "test_longrun_soak.py",
        "test_extended_longrun_soak.py",
    ]

    _LIVE_SOAK_FILES = [
        "test_soak.py",
    ]

    @pytest.fixture(
        params=_FAKE_ONLY_SOAK_FILES,
        ids=_FAKE_ONLY_SOAK_FILES,
    )
    def fake_soak_file(self, request: Any) -> Path:
        """Parametrized fixture for each fake-only soak test file."""
        path = _TESTS_DIR / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_fake_soak_files_no_transport_imports(
        self,
        fake_soak_file: Path,
    ) -> None:
        """Fake-only soak files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            fake_soak_file,
            _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert (
            violations == []
        ), "Fake-only soak file has transport SDK imports:\n" + "\n".join(violations)

    def test_fake_soak_files_no_concrete_adapter_imports(
        self,
        fake_soak_file: Path,
    ) -> None:
        """Fake-only soak files must not import concrete adapter packages."""
        violations = _scan_file_for_banned_imports(
            fake_soak_file,
            (
                "from medre.adapters.matrix",
                "from medre.adapters.meshtastic",
                "from medre.adapters.meshcore",
                "from medre.adapters.lxmf",
            ),
        )
        assert (
            violations == []
        ), "Fake-only soak file has concrete adapter imports:\n" + "\n".join(violations)

    @pytest.mark.parametrize(
        "filename",
        _LIVE_SOAK_FILES,
        ids=_LIVE_SOAK_FILES,
    )
    def test_live_soak_files_have_live_marker(
        self,
        filename: str,
    ) -> None:
        """Live soak files must carry ``pytest.mark.live``."""
        path = _TESTS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found")
        assert _has_live_marker(
            path
        ), f"{filename} imports live SDKs but is missing pytest.mark.live"


# ===================================================================
# 6. No live tests run by default
# ===================================================================


class TestNoLiveTestsRunByDefault:
    """Enforce that the default ``pytest`` invocation does not run live
    tests and that all SDK-importing test files carry the live marker.
    Also enforces the ``hardware`` marker discipline.
    """

    def test_pytest_config_excludes_live_marker(self) -> None:
        """``pyproject.toml`` must have ``addopts = "-m 'not live'"``."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml not found"
        content = _file_source(pyproject)
        assert "not live" in content, (
            "pyproject.toml addopts must exclude live marker "
            "(expected: addopts = \"-m 'not live'\")"
        )

    def test_pytest_config_excludes_hardware_marker(self) -> None:
        """``pyproject.toml`` must have ``addopts`` excluding ``hardware``."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml not found"
        content = _file_source(pyproject)
        assert "not hardware" in content, (
            "pyproject.toml addopts must exclude hardware marker "
            "(expected: addopts = \"-m 'not live and not docker and not hardware'\")"
        )

    def test_live_marker_registered(self) -> None:
        """``pyproject.toml`` must register the ``live`` marker."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        content = _file_source(pyproject)
        assert (
            "live:" in content
        ), "pyproject.toml must register 'live' marker in markers list"

    def test_hardware_marker_registered(self) -> None:
        """``pyproject.toml`` must register the ``hardware`` marker."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        content = _file_source(pyproject)
        assert (
            "hardware:" in content
        ), "pyproject.toml must register 'hardware' marker in markers list"

    def test_hardware_marker_description_mentions_live(self) -> None:
        """The ``hardware`` marker description must state it implies live."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        content = _file_source(pyproject)
        # Find the hardware marker line and verify it mentions "live"
        for line in content.splitlines():
            if "hardware:" in line:
                assert (
                    "live" in line.lower()
                ), "hardware marker description must state it implies live"
                break
        else:
            pytest.fail("hardware marker not found in pyproject.toml")

    def test_docker_marker_registered(self) -> None:
        """``pyproject.toml`` must register the ``docker`` marker."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        content = _file_source(pyproject)
        assert (
            "docker:" in content
        ), "pyproject.toml must register 'docker' marker in markers list"

    @pytest.mark.parametrize(
        "filename",
        [
            "test_lxmf_live.py",
            "test_matrix_live.py",
            "test_matrix_e2ee_live.py",
            "test_meshtastic_live.py",
            "test_meshcore_live.py",
            "test_soak.py",
        ],
        ids=[
            "test_lxmf_live.py",
            "test_matrix_live.py",
            "test_matrix_e2ee_live.py",
            "test_meshtastic_live.py",
            "test_meshcore_live.py",
            "test_soak.py",
        ],
    )
    def test_live_test_file_has_live_marker(
        self,
        filename: str,
    ) -> None:
        """Known live test files must carry ``pytest.mark.live``."""
        path = _TESTS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found")
        assert _has_live_marker(
            path
        ), f"{filename} is a live test file but is missing pytest.mark.live"

    def test_non_live_test_files_no_sdk_imports(self) -> None:
        """Test files that import SDKs without a live marker are violations.

        Scans all ``test_*.py`` files for SDK imports.  Any file that
        imports an SDK must have ``pytest.mark.live`` in its source.

        This test intentionally ignores boundary test files — they
        reference SDKs in string patterns for scanning, not as actual
        imports.
        """
        _BOUNDARY_TEST_FILES = {
            "test_runtime_durability_boundaries.py",
            "test_architectural_boundaries.py",
            "test_supervision_boundaries.py",
            "test_resource_boundaries.py",
            "test_route_runtime_boundaries.py",
            "test_queue_boundaries.py",
            "test_meshtastic_boundaries.py",
            "test_meshcore_boundaries.py",
            "test_matrix_boundaries.py",
            "test_lxmf_boundaries.py",
            "test_cross_transport_boundaries.py",
            "test_operational_boundaries.py",
            "test_deployment_boundaries.py",
        }

        violations: list[str] = []
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            if path.name in _BOUNDARY_TEST_FILES:
                continue

            source = _file_source(path)
            has_sdk_import = False
            for _i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in _BANNED_SDK_IMPORT_PREFIXES:
                    if pattern in stripped:
                        # Real imports start with the pattern at column 0.
                        if stripped.startswith(pattern):
                            has_sdk_import = True
                            break
                if has_sdk_import:
                    break

            if has_sdk_import and not _has_live_marker(path):
                violations.append(path.name)

        assert (
            violations == []
        ), "Test files import transport SDKs without pytest.mark.live:\n" + "\n".join(
            f"  - {v}" for v in violations
        )


# ===================================================================
# 7. Snapshot module has no transport coupling
# ===================================================================


class TestSnapshotNoTransportCoupling:
    """Runtime snapshot module must not import transport SDKs or concrete
    adapter packages.

    ``medre.runtime.snapshot`` produces plain-dict, JSON-safe, deterministic
    snapshots.  It must remain transport-agnostic so that snapshot generation
    works in any deployment environment.
    """

    def test_snapshot_no_transport_sdks(self) -> None:
        """runtime/snapshot.py must not import transport SDKs."""
        source = _source_of("medre.runtime.snapshot")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"runtime/snapshot.py imports transport SDKs: {banned}"

    def test_snapshot_no_concrete_adapters(self) -> None:
        """runtime/snapshot.py must not import concrete adapter packages."""
        source = _source_of("medre.runtime.snapshot")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"runtime/snapshot.py imports concrete adapter packages: {banned}"

    def test_snapshot_produces_plain_dicts(self) -> None:
        """snapshot source must reference plain-dict guarantees.

        The snapshot module documentation and code should reference
        JSON-safe, plain-dict output.  This is a structural check, not
        a runtime test.
        """
        source = _source_of("medre.runtime.snapshot")
        # Check for deterministic/plain-dict guarantees in docstrings.
        assert (
            "JSON" in source or "json" in source
        ), "runtime/snapshot.py should reference JSON-safe output"
        assert (
            "SDK" in source or "sdk" in source or "transport" in source.lower()
        ), "runtime/snapshot.py should document no-SDK guarantees"

    def test_snapshot_test_files_no_sdk_imports(self) -> None:
        """Snapshot test files must not import transport SDKs."""
        snapshot_test_files = [
            "test_runtime_snapshot.py",
            "test_snapshot_stress.py",
        ]
        violations: list[str] = []
        for filename in snapshot_test_files:
            path = _TESTS_DIR / filename
            if not path.exists():
                continue
            file_violations = _scan_file_for_banned_imports(
                path,
                _BANNED_SDK_IMPORT_PREFIXES,
            )
            for v in file_violations:
                violations.append(f"{filename}: {v}")

        assert (
            violations == []
        ), "Snapshot test files have transport SDK imports:\n" + "\n".join(violations)


# ===================================================================
# 8. Hardware marker discipline
# ===================================================================


class TestHardwareMarkerDiscipline:
    """Enforce that ``hardware``-marked tests also carry ``live`` marker.

    The ``hardware`` marker identifies tests requiring physical hardware
    (serial/BLE Meshtastic radios, etc.).  Hardware tests are a strict
    subset of live tests — they connect to real devices.  Therefore,
    any file that uses ``@pytest.mark.hardware`` must also use
    ``@pytest.mark.live`` (either via ``pytestmark`` or decorator).
    """

    def test_hardware_marked_files_also_have_live_marker(self) -> None:
        """Files using ``pytest.mark.hardware`` must also use ``pytest.mark.live``."""
        violations: list[str] = []
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            if _has_hardware_marker(path) and not _has_live_marker(path):
                violations.append(path.name)

        assert violations == [], (
            "Files use pytest.mark.hardware without pytest.mark.live "
            "(hardware tests must also be live-marked):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_addopts_excludes_all_three_markers(self) -> None:
        """``pyproject.toml`` addopts must exclude live, docker, and hardware."""
        pyproject = _REPO_ROOT / "pyproject.toml"
        content = _file_source(pyproject)
        for marker in ("live", "docker", "hardware"):
            assert (
                f"not {marker}" in content
            ), f"pyproject.toml addopts must exclude '{marker}' marker"
