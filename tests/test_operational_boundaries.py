"""Track 8 — Final operational boundary and scope enforcement tests.

These tests enforce structural invariants that prevent feature creep and
guarantee default non-live behaviour.  They use **source-level text
inspection** (not runtime importing of optional SDKs) and cover:

1. Soak framework files remain fake-only unless explicitly live-marked.
2. Operational evidence helpers/tests/docs do not import transport SDKs.
3. CLI workflow tests remain runtime-layer only.
4. No live tests run by default — ``addopts`` and marker discipline.
5. Diagnostics layer (source + tests) has no transport SDK coupling.
6. Deployment helpers (runner, config sample) do not instantiate SDKs.

Pattern
-------
All tests use source-level text inspection.  This avoids triggering SDK
imports at test collection time and works in environments where some or
all SDKs are not installed.
"""

from __future__ import annotations

import configparser
import importlib
import re
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_architectural_boundaries.py)
# ---------------------------------------------------------------------------

_SDK_PACKAGES = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")
"""Third-party transport SDK package names."""

_ADAPTER_PREFIXES = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)
"""Concrete adapter package prefixes (excludes medre.adapters.base and fake_*)."""

_ADAPTER_COMPAT_MODULES = (
    "medre.adapters.matrix.compat",
    "medre.adapters.meshtastic.compat",
    "medre.adapters.meshcore.compat",
    "medre.adapters.lxmf.compat",
)
"""Adapter compat modules that are ALLOWED to import SDKs internally."""

_TESTS_DIR = Path(__file__).parent
"""Root tests directory."""

# Banned import-line prefixes for SDK packages — used for file-level
# scanning where we want to match actual import statements, not comments
# or string literals in boundary test files.
_BANNED_SDK_IMPORT_PREFIXES = (
    "import nio",
    "import meshtastic",
    "import meshcore",
    "import RNS",
    "import lxmf",
    "from nio",
    "from meshtastic",
    "from meshcore",
    "from RNS",
    "from lxmf",
)

_BANNED_ADAPTER_IMPORT_PREFIXES = (
    "from medre.adapters.matrix",
    "from medre.adapters.meshtastic",
    "from medre.adapters.meshcore",
    "from medre.adapters.lxmf",
)


def _source_of(module_name: str) -> str:
    """Import module and return its source text."""
    mod = importlib.import_module(module_name)
    assert mod.__file__ is not None, f"{module_name} has no __file__"
    with open(mod.__file__) as f:
        return f.read()


def _import_lines(source: str) -> list[str]:
    """Extract all import/from-import lines from source text."""
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def _banned_imports(lines: list[str], banned: tuple[str, ...]) -> list[str]:
    """Return import lines referencing any banned package."""
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
    - ``pytestmark = [ ..., pytest.mark.live, ... ]``
    - ``@pytest.mark.live`` decorator
    """
    source = _file_source(path)
    return bool(
        re.search(r"pytest\.mark\.live", source)
    )


# ===================================================================
# 1. Soak framework fake-only unless explicitly live-marked
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
        self, fake_soak_file: Path,
    ) -> None:
        """Fake-only soak files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            fake_soak_file, _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert violations == [], (
            f"Fake-only soak file has transport SDK imports:\n"
            + "\n".join(violations)
        )

    def test_fake_soak_files_no_concrete_adapter_imports(
        self, fake_soak_file: Path,
    ) -> None:
        """Fake-only soak files must not import concrete adapter packages."""
        violations = _scan_file_for_banned_imports(
            fake_soak_file, _BANNED_ADAPTER_IMPORT_PREFIXES,
        )
        assert violations == [], (
            f"Fake-only soak file has concrete adapter imports:\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize(
        "filename",
        _LIVE_SOAK_FILES,
        ids=_LIVE_SOAK_FILES,
    )
    def test_live_soak_files_have_live_marker(
        self, filename: str,
    ) -> None:
        """Live soak files must carry ``pytest.mark.live``."""
        path = _TESTS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found")
        assert _has_live_marker(path), (
            f"{filename} imports live SDKs but is missing pytest.mark.live"
        )


# ===================================================================
# 2. Operational evidence helpers/tests/docs no direct SDK imports
# ===================================================================


class TestOperationalEvidenceNoDirectSdk:
    """Operational evidence and diagnostics test files must not import
    transport SDKs directly.

    These files exercise observability, diagnostic snapshots, and
    operational contracts.  They must be transport-agnostic and runnable
    without any SDK installed.
    """

    _EVIDENCE_TEST_FILES = [
        "test_diagnostics_realism.py",
        "test_runtime_diagnostics.py",
        "test_track3_diagnostics_refinement.py",
        "test_diagnostic_contract.py",
        "test_route_replay_observability.py",
        "test_runtime_boot_summary.py",
        "test_runtime_snapshot.py",
        "test_snapshot_stress.py",
    ]

    _EVIDENCE_SOURCE_MODULES = [
        "medre.core.diagnostics",
        "medre.core.diagnostics.replay_metrics",
        "medre.core.diagnostics.snapshot",
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.diagnostic_contract",
        "medre.core.runtime.health",
        "medre.runtime.snapshot",
        "medre.runtime.boot_summary",
    ]

    @pytest.fixture(
        params=_EVIDENCE_TEST_FILES,
        ids=_EVIDENCE_TEST_FILES,
    )
    def evidence_test_file(self, request: Any) -> Path:
        """Parametrized fixture for each evidence test file."""
        path = _TESTS_DIR / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_evidence_test_files_no_sdk_imports(
        self, evidence_test_file: Path,
    ) -> None:
        """Evidence test files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            evidence_test_file, _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert violations == [], (
            f"Evidence test file has transport SDK imports:\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize(
        "module_name",
        _EVIDENCE_SOURCE_MODULES,
    )
    def test_evidence_source_modules_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """Evidence source modules must not import transport SDKs."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _EVIDENCE_SOURCE_MODULES,
    )
    def test_evidence_source_modules_no_concrete_adapter_imports(
        self, module_name: str,
    ) -> None:
        """Evidence source modules must not import concrete adapter packages."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter packages: {banned}"
        )


# ===================================================================
# 3. CLI workflow tests remain runtime-layer only
# ===================================================================


class TestCliWorkflowsRuntimeLayerOnly:
    """CLI workflow test files must not import transport SDKs or concrete
    adapter packages.

    CLI tests exercise the command-line interface through fake adapters
    and config-only paths.  They validate operator workflows without
    requiring live transports.
    """

    _CLI_TEST_FILES = [
        "test_cli.py",
        "test_operator_workflows.py",
        "test_operator_failures.py",
    ]

    _CLI_SOURCE_MODULES = [
        "medre.cli",
    ]

    @pytest.fixture(
        params=_CLI_TEST_FILES,
        ids=_CLI_TEST_FILES,
    )
    def cli_test_file(self, request: Any) -> Path:
        """Parametrized fixture for each CLI test file."""
        path = _TESTS_DIR / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_cli_test_files_no_sdk_imports(
        self, cli_test_file: Path,
    ) -> None:
        """CLI test files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            cli_test_file, _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert violations == [], (
            f"CLI test file has transport SDK imports:\n"
            + "\n".join(violations)
        )

    # Adapter config modules are pure data classes — they import no SDKs
    # and are safe for CLI tests to use.  Only ban imports of adapter
    # runtime modules (session, adapter, codec, etc.).
    _BANNED_CLI_ADAPTER_IMPORTS = (
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

    def test_cli_test_files_no_concrete_adapter_imports(
        self, cli_test_file: Path,
    ) -> None:
        """CLI test files must not import concrete adapter runtime modules.

        Config imports (``medre.adapters.*.config``) are permitted — they
        are pure data classes with no SDK dependency.
        """
        violations = _scan_file_for_banned_imports(
            cli_test_file, self._BANNED_CLI_ADAPTER_IMPORTS,
        )
        assert violations == [], (
            f"CLI test file has concrete adapter runtime imports:\n"
            + "\n".join(violations)
        )

    @pytest.mark.parametrize(
        "module_name",
        _CLI_SOURCE_MODULES,
    )
    def test_cli_source_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """CLI source modules must not have top-level SDK imports."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _CLI_SOURCE_MODULES,
    )
    def test_cli_source_no_concrete_adapter_imports(
        self, module_name: str,
    ) -> None:
        """CLI source modules must not import concrete adapter packages."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter packages: {banned}"
        )


# ===================================================================
# 4. No live tests run by default
# ===================================================================


class TestNoLiveTestsRunByDefault:
    """Enforce that the default ``pytest`` invocation does not run live
    tests and that all SDK-importing test files carry the live marker.
    """

    def test_pytest_config_excludes_live_marker(self) -> None:
        """``pyproject.toml`` must have ``addopts = "-m 'not live'"``."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        assert pyproject.exists(), "pyproject.toml not found"
        content = _file_source(pyproject)
        assert "not live" in content, (
            "pyproject.toml addopts must exclude live marker "
            "(expected: addopts = \"-m 'not live'\")"
        )

    def test_live_marker_registered(self) -> None:
        """``pyproject.toml`` must register the ``live`` marker."""
        pyproject = _TESTS_DIR.parent / "pyproject.toml"
        content = _file_source(pyproject)
        assert "live:" in content, (
            "pyproject.toml must register 'live' marker in markers list"
        )

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
        self, filename: str,
    ) -> None:
        """Known live test files must carry ``pytest.mark.live``."""
        path = _TESTS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found")
        assert _has_live_marker(path), (
            f"{filename} is a live test file but is missing pytest.mark.live"
        )

    def test_non_live_test_files_no_sdk_imports(self) -> None:
        """Test files that import SDKs without a live marker are violations.

        Scans all ``test_*.py`` files for SDK imports.  Any file that
        imports an SDK must have ``pytest.mark.live`` in its source.

        This test intentionally ignores:
        - ``test_*_boundaries.py`` files (they reference SDKs in string
          patterns for scanning, not as actual imports).
        - ``test_operational_boundaries.py`` (this file).
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
            for i, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for pattern in _BANNED_SDK_IMPORT_PREFIXES:
                    if pattern in stripped:
                        # Exclude lines that are clearly string data
                        # (e.g., '"import nio"' or pattern definitions).
                        # Real imports start with the pattern at column 0.
                        if stripped.startswith(pattern):
                            has_sdk_import = True
                            break
                if has_sdk_import:
                    break

            if has_sdk_import and not _has_live_marker(path):
                violations.append(path.name)

        assert violations == [], (
            "Test files import transport SDKs without pytest.mark.live:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


# ===================================================================
# 5. Diagnostics layer no transport coupling
# ===================================================================


class TestDiagnosticsNoTransportCoupling:
    """Diagnostics source modules and their test files must not import
    transport SDKs or concrete adapter packages.

    This extends the checks in ``test_runtime_durability_boundaries.py``
    (section 1) to cover the full diagnostics subsystem including
    ``core.runtime.diagnostics``, ``core.runtime.health``,
    ``core.runtime.diagnostic_contract``, and ``core.runtime.supervision``.
    """

    _DIAGNOSTICS_SOURCE_MODULES = [
        "medre.core.diagnostics",
        "medre.core.diagnostics.replay_metrics",
        "medre.core.diagnostics.snapshot",
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.diagnostic_contract",
        "medre.core.runtime.health",
        "medre.core.runtime.supervision",
        "medre.core.runtime.accounting",
        "medre.core.runtime.capabilities",
    ]

    _DIAGNOSTICS_TEST_FILES = [
        "test_runtime_diagnostics.py",
        "test_diagnostics_realism.py",
        "test_track3_diagnostics_refinement.py",
        "test_diagnostic_contract.py",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _DIAGNOSTICS_SOURCE_MODULES,
    )
    def test_diagnostics_source_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """Diagnostics source modules must not import transport SDKs."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _DIAGNOSTICS_SOURCE_MODULES,
    )
    def test_diagnostics_source_no_concrete_adapter_imports(
        self, module_name: str,
    ) -> None:
        """Diagnostics source modules must not import concrete adapter
        packages.

        Imports from ``medre.adapters.base`` (protocol types) are
        permitted — these are abstract interfaces, not concrete adapters.
        """
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter packages: {banned}"
        )

    @pytest.fixture(
        params=_DIAGNOSTICS_TEST_FILES,
        ids=_DIAGNOSTICS_TEST_FILES,
    )
    def diagnostics_test_file(self, request: Any) -> Path:
        """Parametrized fixture for each diagnostics test file."""
        path = _TESTS_DIR / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_diagnostics_test_files_no_sdk_imports(
        self, diagnostics_test_file: Path,
    ) -> None:
        """Diagnostics test files must not import transport SDKs."""
        violations = _scan_file_for_banned_imports(
            diagnostics_test_file, _BANNED_SDK_IMPORT_PREFIXES,
        )
        assert violations == [], (
            f"Diagnostics test file has transport SDK imports:\n"
            + "\n".join(violations)
        )


# ===================================================================
# 6. Deployment helpers no direct SDK instantiation
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

    _SDK_INSTANTIATION_PATTERNS = (
        "nio.AsyncClient(",
        "MeshtasticClient(",
        "MeshCore(",
        "RNS.Reticulum(",
        "LXMF.LXMF(",
        "lxmf.LXMF(",
    )

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """Deployment modules must not have top-level SDK imports."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_concrete_adapter_imports(
        self, module_name: str,
    ) -> None:
        """Deployment modules must not import concrete adapter packages."""
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter packages: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _DEPLOYMENT_SOURCE_MODULES,
    )
    def test_deployment_modules_no_sdk_instantiation(
        self, module_name: str,
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

        assert violations == [], (
            f"{module_name} directly instantiates transport SDKs:\n"
            + "\n".join(violations)
        )

    def test_run_commands_uses_builder_pattern(self) -> None:
        """``medre.cli.run_commands`` must build the runtime through
        ``RuntimeBuilder`` — never through direct adapter construction."""
        source = _source_of("medre.cli.run_commands")
        assert "RuntimeBuilder" in source, (
            "medre.cli.run_commands must use RuntimeBuilder for runtime assembly"
        )

    def test_config_sample_no_sdk_references(self) -> None:
        """``medre.config.sample`` must not reference SDK modules in code.

        The generated TOML config may reference adapter section names
        (e.g., ``[adapters.matrix.main]``), but the Python source must
        not import or reference SDK modules.
        """
        source = _source_of("medre.config.sample")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"medre.config.sample imports transport SDKs: {banned}"
        )


# ===================================================================
# 7. Core logging imports canonical sanitizer
# ===================================================================


class TestCoreLoggingImportsCanonicalSanitizer:
    """``medre.core.observability.logging`` must not define its own
    redaction logic — it must import and use the canonical
    ``sanitize_for_log`` from ``medre.observability.sanitization``.
    """

    _LOGGING_MODULE = "medre.core.observability.logging"

    def test_no_sensitive_keys_definition(self) -> None:
        """``_SENSITIVE_KEYS`` must not be defined in the logging module."""
        source = _source_of(self._LOGGING_MODULE)
        assert "_SENSITIVE_KEYS" not in source, (
            f"{self._LOGGING_MODULE} still defines _SENSITIVE_KEYS — "
            "remove it and use canonical sanitize_for_log"
        )

    def test_no_redact_value_definition(self) -> None:
        """``_redact_value`` must not be defined in the logging module."""
        source = _source_of(self._LOGGING_MODULE)
        assert "_redact_value" not in source, (
            f"{self._LOGGING_MODULE} still defines _redact_value — "
            "remove it and use canonical sanitize_for_log"
        )

    def test_no_redact_context_definition(self) -> None:
        """``_redact_context`` must not be defined in the logging module."""
        source = _source_of(self._LOGGING_MODULE)
        assert "_redact_context" not in source, (
            f"{self._LOGGING_MODULE} still defines _redact_context — "
            "remove it and use canonical sanitize_for_log"
        )

    def test_no_redacted_constant(self) -> None:
        """``_REDACTED`` must not be defined in the logging module."""
        source = _source_of(self._LOGGING_MODULE)
        assert "_REDACTED" not in source, (
            f"{self._LOGGING_MODULE} still defines _REDACTED — "
            "remove it and use canonical sanitize_for_log"
        )

    def test_imports_canonical_sanitize_for_log(self) -> None:
        """``sanitize_for_log`` must be imported from the canonical location."""
        source = _source_of(self._LOGGING_MODULE)
        assert (
            "from medre.observability.sanitization import" in source
            and "sanitize_for_log" in source
        ), (
            f"{self._LOGGING_MODULE} must import sanitize_for_log "
            "from medre.observability.sanitization"
        )

    def test_uses_canonical_sanitize_for_log(self) -> None:
        """The module must call ``sanitize_for_log`` (not a local helper)."""
        source = _source_of(self._LOGGING_MODULE)
        assert "sanitize_for_log(" in source, (
            f"{self._LOGGING_MODULE} must use sanitize_for_log()"
        )
