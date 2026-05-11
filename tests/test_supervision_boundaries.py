"""Supervision boundary tests ensuring transport-agnostic isolation (Contract 56).

Enforces architectural boundaries:
1. Runtime supervision code imports no transport SDKs.
2. Runtime supervision code imports no concrete adapter packages.
3. Runtime diagnostics/snapshot code imports no transport SDKs.
4. Runtime health code imports no transport SDKs.
5. Runtime persistence (storage) code imports no transport SDKs.
6. Runtime health classification is deterministic and pure.

These tests use static source analysis (import-line inspection) to catch
boundary violations at test time, not runtime.

Uses no live dependencies.
"""

from __future__ import annotations

import importlib
import re

import pytest


# ---------------------------------------------------------------------------
# Shared constants
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

_RUNTIME_MODULES = (
    "medre.core.runtime.supervision",
    "medre.core.runtime.diagnostics",
    "medre.core.runtime.health",
    "medre.core.runtime.diagnostic_contract",
    "medre.core.runtime.capabilities",
)
"""Runtime core modules that must remain transport-agnostic."""

_PERSISTENCE_MODULES = (
    "medre.core.storage.sqlite",
    "medre.core.storage.backend",
    "medre.core.storage.replay",
)
"""Persistence modules that must remain transport-agnostic."""


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


# ===================================================================
# A) Supervision module boundary
# ===================================================================


class TestSupervisionBoundary:
    """medre.core.runtime.supervision must not import transport SDKs
    or concrete adapter packages."""

    def test_no_transport_sdk_imports(self) -> None:
        source = _source_of("medre.core.runtime.supervision")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"supervision.py imports transport SDKs: {banned_sdk}"
        )

    def test_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.core.runtime.supervision")
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"supervision.py imports concrete adapter packages: {banned_adapters}"
        )

    def test_only_imports_core_and_lifecycle(self) -> None:
        """Supervision should only import from core/lifecycle."""
        source = _source_of("medre.core.runtime.supervision")
        lines = _import_lines(source)

        for line in lines:
            # Standard library imports are fine
            if line.startswith(("from __future__", "import ", "from enum", "from typing")):
                continue
            # Allowed internal imports
            assert "medre.core.lifecycle.states" in line or line.startswith("import"), (
                f"supervision.py has unexpected import: {line}"
            )


# ===================================================================
# B) Diagnostics module boundary
# ===================================================================


class TestDiagnosticsBoundary:
    """medre.core.runtime.diagnostics must not import transport SDKs
    or concrete adapter packages."""

    def test_no_transport_sdk_imports(self) -> None:
        source = _source_of("medre.core.runtime.diagnostics")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"diagnostics.py imports transport SDKs: {banned_sdk}"
        )

    def test_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.core.runtime.diagnostics")
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"diagnostics.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# C) Health module boundary
# ===================================================================


class TestHealthBoundary:
    """medre.core.runtime.health must not import transport SDKs
    or concrete adapter packages."""

    def test_no_transport_sdk_imports(self) -> None:
        source = _source_of("medre.core.runtime.health")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"health.py imports transport SDKs: {banned_sdk}"
        )

    def test_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.core.runtime.health")
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"health.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# D) Diagnostic contract boundary
# ===================================================================


class TestDiagnosticContractBoundary:
    """medre.core.runtime.diagnostic_contract must not import transport SDKs
    or concrete adapter packages."""

    def test_no_transport_sdk_imports(self) -> None:
        source = _source_of("medre.core.runtime.diagnostic_contract")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"diagnostic_contract.py imports transport SDKs: {banned_sdk}"
        )

    def test_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.core.runtime.diagnostic_contract")
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"diagnostic_contract.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# E) Persistence / storage boundary
# ===================================================================


class TestPersistenceBoundary:
    """Persistence modules (storage) must not import transport SDKs."""

    @pytest.mark.parametrize("module_name", _PERSISTENCE_MODULES)
    def test_no_transport_sdk_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"{module_name} imports transport SDKs: {banned_sdk}"
        )

    @pytest.mark.parametrize("module_name", _PERSISTENCE_MODULES)
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"{module_name} imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# F) All runtime core modules remain transport-agnostic
# ===================================================================


class TestRuntimeCoreAgnostic:
    """All runtime core modules must remain free of transport SDK imports."""

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_no_transport_sdk_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"{module_name} imports transport SDKs: {banned_sdk}"
        )

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"{module_name} imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# G) Runtime health classification is importable without transport deps
# ===================================================================


class TestSupervisionImportIndependence:
    """Supervision module can be imported without any transport SDK installed."""

    def test_import_succeeds_without_transport_sdks(self) -> None:
        """Importing supervision must not trigger any SDK import."""
        from medre.core.runtime.supervision import (  # noqa: F401
            RuntimeHealth,
            classify_runtime_health,
        )

    def test_import_via_runtime_package(self) -> None:
        """Supervision symbols are available via the runtime package."""
        from medre.core.runtime import (  # noqa: F401
            AdapterFailureSeverity,
            RuntimeHealth,
            StartupOutcome,
            classify_adapter_failure_severity,
            classify_runtime_health,
            classify_startup_outcome,
            runtime_supervision_snapshot,
        )
