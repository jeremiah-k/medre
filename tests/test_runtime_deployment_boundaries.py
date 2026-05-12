"""Track 9 — Runtime-level deployment boundary enforcement tests.

These tests enforce structural invariants that guarantee the runtime
subsystem remains transport-agnostic and deployment-clean.  They use
**source-level text inspection** (not runtime importing of optional SDKs).

Complement: ``tests/test_deployment_boundaries.py`` covers Track 8
(deployment helpers, CLI, clean-env tests, soak, no-live-by-default).
This file covers runtime-level boundaries:

1. Runtime core modules (app, builder, capacity) do not import transport
   SDKs or concrete adapter runtime modules (adapter, session, codec).
2. Runtime core modules do not instantiate SDK objects.
3. Snapshot modules (runtime/snapshot, core/diagnostics/snapshot) are
   SDK-free and transport-agnostic.
4. Observability modules are SDK-free.
5. Export/reporting modules are SDK-free.
6. Runtime builder uses BaseAdapter abstraction — no direct adapter
   construction.
7. Runtime modules reference adapter config dataclasses (pure frozen
   dataclasses) but not adapter runtime modules.

Pattern
-------
All tests use source-level text inspection.  This avoids triggering SDK
imports at test collection time and works in environments where some or
all SDKs are not installed.

Adapter config dataclasses (``medre.adapters.*.config``) are pure frozen
dataclasses with no SDK dependency.  Imports of these modules are **not**
flagged as violations — only runtime modules (adapter, session, codec)
and direct SDK imports are banned.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as test_deployment_boundaries.py)
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

_ADAPTER_CONFIG_ALLOWED = (
    "medre.adapters.matrix.config",
    "medre.adapters.meshtastic.config",
    "medre.adapters.meshcore.config",
    "medre.adapters.lxmf.config",
)
"""Adapter config modules that ARE allowed — pure dataclasses, no SDK."""

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

# Adapter runtime module imports banned in runtime core contexts.
# Config imports (medre.adapters.*.config) are pure dataclasses — permitted.
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


# ===================================================================
# 1. Runtime core modules do not import transport SDKs
# ===================================================================


class TestRuntimeCoreNoSdk:
    """Runtime core modules must not import transport SDKs.

    The runtime layer (app, builder, capacity, snapshot, observability,
    boot_summary, errors, routes, route_engine) is consumed by the CLI
    and runner in clean environments.  It must never depend on optional
    transport SDK packages.

    Note: ``medre.runtime.builder`` imports adapter config dataclasses
    (``medre.adapters.*.config``) and the abstract ``BaseAdapter``.
    These are pure dataclasses / abstract base with no SDK dependency
    and are excluded from the SDK ban.
    """

    _RUNTIME_MODULES = [
        "medre.runtime.app",
        "medre.runtime.builder",
        "medre.runtime.capacity",
        "medre.runtime.snapshot",
        "medre.runtime.observability",
        "medre.runtime.boot_summary",
        "medre.runtime.errors",
        "medre.runtime.routes",
        "medre.runtime.route_engine",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _RUNTIME_MODULES,
    )
    def test_runtime_modules_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """Runtime modules must not have top-level SDK imports."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _RUNTIME_MODULES,
    )
    def test_runtime_modules_no_sdk_instantiation(
        self, module_name: str,
    ) -> None:
        """Runtime modules must not directly instantiate SDK objects."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")

        instantiation_patterns = (
            "nio.AsyncClient(",
            "MeshtasticClient(",
            "MeshCore(",
            "RNS.Reticulum(",
            "LXMF.LXMF(",
            "lxmf.LXMF(",
        )
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in instantiation_patterns:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert violations == [], (
            f"{module_name} directly instantiates transport SDKs:\n"
            + "\n".join(violations)
        )


# ===================================================================
# 2. Runtime core modules do not import adapter runtime modules
# ===================================================================


class TestRuntimeCoreNoAdapterRuntime:
    """Runtime core modules must not import concrete adapter runtime modules.

    The runtime builder uses ``BaseAdapter`` and adapter config dataclasses
    to construct adapters through abstraction.  It must never import
    concrete adapter modules (adapter.py, session.py, codec.py, queue.py)
    directly — those are loaded via compat modules and dynamic imports.

    This does NOT ban:
    - ``from medre.adapters.base import BaseAdapter`` (abstract base)
    - ``from medre.adapters.matrix.config import MatrixConfig`` (pure dataclass)
    - ``from medre.adapters.fake_adapter import FakeAdapter`` (test utility)
    """

    _RUNTIME_MODULES = [
        "medre.runtime.app",
        "medre.runtime.builder",
        "medre.runtime.capacity",
        "medre.runtime.snapshot",
        "medre.runtime.observability",
        "medre.runtime.boot_summary",
        "medre.runtime.errors",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _RUNTIME_MODULES,
    )
    def test_runtime_modules_no_adapter_runtime_imports(
        self, module_name: str,
    ) -> None:
        """Runtime modules must not import adapter runtime modules.

        Config dataclass imports (``medre.adapters.*.config``) are
        permitted — they are pure frozen dataclasses with no SDK
        dependency.
        """
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        # Filter out config imports and base imports — those are allowed.
        allowed_lines = []
        for line in lines:
            if line.startswith("from medre.adapters."):
                # Allow base imports and config imports
                if ".base " in line or ".config " in line:
                    continue
                # Allow fake_adapter imports
                if "fake_adapter" in line:
                    continue
            allowed_lines.append(line)

        banned = _banned_imports(allowed_lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter runtime modules: {banned}"
        )


# ===================================================================
# 3. Snapshot modules are SDK-free and transport-agnostic
# ===================================================================


class TestSnapshotModulesSdkFree:
    """Snapshot modules must not import transport SDKs or concrete adapters.

    Both ``medre.runtime.snapshot`` and ``medre.core.diagnostics.snapshot``
    produce plain-dict, JSON-safe, deterministic snapshots.  They must
    remain transport-agnostic so that snapshot generation works in any
    deployment environment.
    """

    _SNAPSHOT_MODULES = [
        "medre.runtime.snapshot",
        "medre.core.diagnostics.snapshot",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _SNAPSHOT_MODULES,
    )
    def test_snapshot_no_transport_sdks(
        self, module_name: str,
    ) -> None:
        """Snapshot modules must not import transport SDKs."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )

    @pytest.mark.parametrize(
        "module_name",
        _SNAPSHOT_MODULES,
    )
    def test_snapshot_no_concrete_adapters(
        self, module_name: str,
    ) -> None:
        """Snapshot modules must not import concrete adapter packages."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], (
            f"{module_name} imports concrete adapter packages: {banned}"
        )

    def test_runtime_snapshot_produces_plain_dicts(self) -> None:
        """runtime/snapshot.py must reference plain-dict guarantees."""
        try:
            source = _source_of("medre.runtime.snapshot")
        except ImportError:
            pytest.skip("medre.runtime.snapshot not importable")

        assert "JSON" in source or "json" in source, (
            "runtime/snapshot.py should reference JSON-safe output"
        )
        assert "SDK" in source or "sdk" in source or "No SDK" in source, (
            "runtime/snapshot.py should document no-SDK guarantees"
        )

    def test_core_snapshot_produces_plain_dicts(self) -> None:
        """core/diagnostics/snapshot.py must reference plain-dict guarantees."""
        try:
            source = _source_of("medre.core.diagnostics.snapshot")
        except ImportError:
            pytest.skip("medre.core.diagnostics.snapshot not importable")

        assert "JSON" in source or "json" in source, (
            "core/diagnostics/snapshot.py should reference JSON-safe output"
        )
        assert "SDK" in source or "sdk" in source or "No raw SDK" in source, (
            "core/diagnostics/snapshot.py should document no-SDK guarantees"
        )


# ===================================================================
# 4. Observability modules are SDK-free
# ===================================================================


class TestObservabilityModulesSdkFree:
    """Observability modules must not import transport SDKs.

    ``medre.runtime.observability`` (DiagnosticsCollector) and
    ``medre.core.observability.*`` provide metrics and logging.  They
    must remain transport-agnostic.
    """

    _OBSERVABILITY_MODULES = [
        "medre.runtime.observability",
        "medre.core.observability.metrics",
        "medre.core.observability.logging",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _OBSERVABILITY_MODULES,
    )
    def test_observability_no_transport_sdks(
        self, module_name: str,
    ) -> None:
        """Observability modules must not import transport SDKs."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )


# ===================================================================
# 5. Runtime builder uses BaseAdapter abstraction
# ===================================================================


class TestBuilderAbstraction:
    """RuntimeBuilder must construct adapters through abstraction.

    The builder may import ``BaseAdapter`` and adapter config dataclasses,
    but must never directly instantiate concrete adapter classes.
    """

    def test_builder_imports_base_adapter(self) -> None:
        """builder.py must import BaseAdapter from the base module."""
        try:
            source = _source_of("medre.runtime.builder")
        except ImportError:
            pytest.skip("medre.runtime.builder not importable")

        assert "BaseAdapter" in source, (
            "medre.runtime.builder must reference BaseAdapter"
        )

    def test_builder_no_direct_adapter_construction(self) -> None:
        """builder.py must not directly construct concrete adapter classes.

        The builder may construct FakeAdapter instances (test utilities)
        and use factory patterns, but must not call constructors of
        real adapter classes (MatrixAdapter, MeshtasticAdapter, etc.)
        that depend on transport SDKs.
        """
        try:
            source = _source_of("medre.runtime.builder")
        except ImportError:
            pytest.skip("medre.runtime.builder not importable")

        # Direct construction patterns banned:
        # MatrixAdapter(...), MeshtasticAdapter(...), etc.
        # But Fake*Adapter() is allowed — those are test utilities.
        banned_constructions = (
            " MatrixAdapter(",
            " MeshtasticAdapter(",
            " MeshCoreAdapter(",
            " LxmfAdapter(",
            ", MatrixAdapter(",
            ", MeshtasticAdapter(",
            ", MeshCoreAdapter(",
            ", LxmfAdapter(",
        )
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Skip string literals in factory definitions
            if stripped.startswith('"') or stripped.startswith("'"):
                continue
            for pattern in banned_constructions:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert violations == [], (
            "medre.runtime.builder directly constructs concrete adapters:\n"
            + "\n".join(violations)
        )

    def test_builder_imports_config_dataclasses(self) -> None:
        """builder.py must import adapter config dataclasses (allowed pattern)."""
        try:
            source = _source_of("medre.runtime.builder")
        except ImportError:
            pytest.skip("medre.runtime.builder not importable")

        # Builder should import config dataclasses — this is the permitted pattern
        assert "MatrixRuntimeConfig" in source or "matrix.config" in source, (
            "medre.runtime.builder should import adapter config dataclasses"
        )


# ===================================================================
# 6. Core modules are transport-agnostic
# ===================================================================


class TestCoreModulesTransportAgnostic:
    """Core engine and storage modules must not import transport SDKs.

    These modules (pipeline, storage, events, routing, rendering,
    lifecycle, health, accounting, supervision, diagnostics) form the
    MEDRE infrastructure layer and must remain transport-agnostic.
    """

    _CORE_MODULES = [
        "medre.core.engine.pipeline",
        "medre.core.storage.sqlite",
        "medre.core.storage.replay",
        "medre.core.storage.backend",
        "medre.core.events.bus",
        "medre.core.events.canonical",
        "medre.core.events.schema",
        "medre.core.events.metadata",
        "medre.core.events.kinds",
        "medre.core.routing.router",
        "medre.core.routing.stats",
        "medre.core.routing.models",
        "medre.core.rendering.renderer",
        "medre.core.rendering.text",
        "medre.core.lifecycle.manager",
        "medre.core.lifecycle.states",
        "medre.core.runtime.accounting",
        "medre.core.runtime.capabilities",
        "medre.core.runtime.diagnostics",
        "medre.core.runtime.health",
        "medre.core.runtime.supervision",
        "medre.core.runtime.diagnostic_contract",
        "medre.core.diagnostics.replay_metrics",
        "medre.core.observability.metrics",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _CORE_MODULES,
    )
    def test_core_modules_no_sdk_imports(
        self, module_name: str,
    ) -> None:
        """Core modules must not have top-level SDK imports."""
        try:
            source = _source_of(module_name)
        except ImportError:
            pytest.skip(f"{module_name} not importable")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], (
            f"{module_name} imports transport SDKs: {banned}"
        )


# ===================================================================
# 7. Export/reporting test files are SDK-free
# ===================================================================


class TestExportReportingTestsSdkFree:
    """Test files for snapshot, export, and reporting must not import SDKs.

    These tests exercise the deterministic snapshot and reporting
    infrastructure and should work in clean environments.
    """

    _TESTS_DIR = Path(__file__).parent

    _EXPORT_TEST_FILES = [
        "test_runtime_snapshot.py",
        "test_snapshot_stress.py",
        "test_runtime_diagnostics.py",
        "test_runtime_accounting.py",
        "test_runtime_boot_summary.py",
        "test_runtime_hygiene.py",
        "test_runtime_recovery.py",
        "test_runtime_cancellation.py",
        "test_runtime_event_flow.py",
        "test_supervision_boundaries.py",
        "test_resource_containment.py",
        "test_resource_boundaries.py",
    ]

    @pytest.mark.parametrize(
        "filename",
        _EXPORT_TEST_FILES,
        ids=_EXPORT_TEST_FILES,
    )
    def test_export_test_files_no_sdk_imports(
        self, filename: str,
    ) -> None:
        """Export/reporting test files must not import transport SDKs.

        Boundary test files (test_runtime_durability_boundaries.py,
        test_deployment_boundaries.py) are excluded because they
        reference SDK names in string scanning patterns, not as actual
        imports.
        """
        path = self._TESTS_DIR / filename
        if not path.exists():
            pytest.skip(f"{filename} not found")

        source = _file_source(path)
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in _BANNED_SDK_IMPORT_PREFIXES:
                if pattern in stripped:
                    violations.append(f"{filename}:{i}: {stripped}")
                    break

        assert violations == [], (
            f"Export/reporting test file has transport SDK imports:\n"
            + "\n".join(violations)
        )


# ===================================================================
# 8. Documentation boundary test — Track 8/9 headers present
# ===================================================================


class TestDocumentationTrackHeaders:
    """Verify that key documentation files have Track 8/9 headers.

    This is a lightweight structural check to prevent drift where
    documentation files lose their track classification.
    """

    _REPO_ROOT = Path(__file__).parent.parent

    _TRACKED_DOCS = [
        ("docs/runbooks/live-operational-evidence.md", ["Track"]),
        ("docs/runbooks/deployment-validation.md", ["Track"]),
        ("docs/runbooks/container-operation.md", ["Track"]),
        ("docs/runbooks/longrun-validation.md", ["Track"]),
    ]

    @pytest.mark.parametrize(
        "doc_path,required_keywords",
        _TRACKED_DOCS,
        ids=[p[0] for p in _TRACKED_DOCS],
    )
    def test_doc_has_track_header(
        self, doc_path: str, required_keywords: list[str],
    ) -> None:
        """Runbook documentation file must contain Track classification in header."""
        full_path = self._REPO_ROOT / doc_path
        if not full_path.exists():
            pytest.skip(f"{doc_path} not found")

        source = _file_source(full_path)
        # Check the first 30 lines for track references
        header = "\n".join(source.splitlines()[:30])
        for keyword in required_keywords:
            assert keyword in header or keyword.lower() in header.lower(), (
                f"{doc_path} is missing '{keyword}' in the first 30 lines. "
                f"Add a Tracks header to prevent drift."
            )

    @pytest.mark.parametrize(
        "doc_path,required_keywords",
        _TRACKED_DOCS,
        ids=[p[0] for p in _TRACKED_DOCS],
    )
    def test_doc_has_last_updated(
        self, doc_path: str, required_keywords: list[str],
    ) -> None:
        """Runbook documentation file must contain 'Last updated' date in header."""
        full_path = self._REPO_ROOT / doc_path
        if not full_path.exists():
            pytest.skip(f"{doc_path} not found")

        source = _file_source(full_path)
        header = "\n".join(source.splitlines()[:30])
        assert "Last updated" in header, (
            f"{doc_path} is missing 'Last updated' date in the first 30 lines. "
            f"Add a date header to track freshness."
        )


# ===================================================================
# Contract header check — Tracks in bold metadata
# ===================================================================


class TestContractTrackHeaders:
    """Verify that contract documents have Track references in metadata.

    Contracts use bold metadata (``**Tracks:**``) rather than blockquote
    format (``> Tracks:``).  Both formats are accepted.
    """

    _REPO_ROOT = Path(__file__).parent.parent

    _CONTRACT_DOCS = [
        ("docs/contracts/59-runtime-durability-contract.md", ["Track"]),
        ("docs/contracts/60-runtime-cancellation-contract.md", ["Track"]),
        ("docs/contracts/61-operational-evidence-contract.md", ["Track"]),
    ]

    @pytest.mark.parametrize(
        "doc_path,required_keywords",
        _CONTRACT_DOCS,
        ids=[p[0] for p in _CONTRACT_DOCS],
    )
    def test_contract_has_track_header(
        self, doc_path: str, required_keywords: list[str],
    ) -> None:
        """Contract must contain Track classification in header metadata."""
        full_path = self._REPO_ROOT / doc_path
        if not full_path.exists():
            pytest.skip(f"{doc_path} not found")

        source = _file_source(full_path)
        # Contracts may use blockquote (>) or bold (**) format
        header = "\n".join(source.splitlines()[:20])
        for keyword in required_keywords:
            assert keyword in header or keyword.lower() in header.lower(), (
                f"{doc_path} is missing '{keyword}' in the first 20 lines. "
                f"Add a Track reference to prevent drift."
            )


# ===================================================================
# 9. No live tests run by default (complementary check)
# ===================================================================


class TestNoLiveTestsByDefaultComplementary:
    """Complementary check to test_deployment_boundaries.py::TestNoLiveTestsRunByDefault.

    Verifies that runtime test files do not import SDKs without live markers.
    """

    _TESTS_DIR = Path(__file__).parent

    _RUNTIME_TEST_FILES = [
        "test_runtime_snapshot.py",
        "test_runtime_diagnostics.py",
        "test_runtime_accounting.py",
        "test_runtime_boot_summary.py",
        "test_runtime_hygiene.py",
        "test_runtime_recovery.py",
        "test_runtime_builder.py",
        "test_runtime_cancellation.py",
        "test_runtime_event_flow.py",
        "test_runtime_routing.py",
    ]

    def test_runtime_test_files_no_live_marker(self) -> None:
        """Runtime test files must NOT have the live marker.

        Runtime tests are deterministic and must work without any
        transport SDK or live endpoint.

        Boundary test files (test_deployment_boundaries.py,
        test_runtime_deployment_boundaries.py) are excluded because
        they reference ``pytest.mark.live`` in scanning patterns, not
        as actual test markers.
        """
        violations: list[str] = []
        for filename in self._RUNTIME_TEST_FILES:
            path = self._TESTS_DIR / filename
            if not path.exists():
                continue
            source = _file_source(path)
            if re.search(r"pytest\.mark\.live", source):
                violations.append(filename)

        assert violations == [], (
            "Runtime test files have live markers (they should be deterministic):\n"
            + "\n".join(f"  - {v}" for v in violations)
        )
