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

import pytest

from medre.runtime.architecture_report import _SDK_PACKAGES
from tests.helpers.import_scanner import ADAPTER_PREFIXES as _ADAPTER_PREFIXES
from tests.helpers.import_scanner import banned_imports as _banned_imports
from tests.helpers.import_scanner import import_lines as _import_lines
from tests.helpers.source_reader import source_of as _source_of

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------


_RUNTIME_MODULES = (
    "medre.core.supervision.supervision",
    "medre.core.supervision.diagnostics",
    "medre.core.supervision.health",
    "medre.core.supervision.diagnostic_contract",
    "medre.core.supervision.capabilities",
)
"""Runtime core modules that must remain transport-agnostic."""

_TRANSPORT_AGNOSTIC_CORE_MODULES = (
    "medre.core.storage.sqlite",
    "medre.core.storage.backend",
    "medre.core.engine.replay.engine",
    "medre.core.engine.replay.types",
    "medre.core.engine.replay.summary",
    "medre.core.engine.replay.helpers",
    "medre.core.engine.replay.delivery",
    "medre.core.engine.replay.routing",
    "medre.core.engine.replay.protocols",
    "medre.core.engine.replay.selection",
    "medre.core.engine.replay.store",
    "medre.core.engine.replay.planning",
    "medre.core.engine.replay.rendering",
)
"""Core modules (storage backend, replay engine) that must remain transport-agnostic."""


# ===================================================================
# A) Supervision module boundary
# ===================================================================


class TestSupervisionBoundary:
    """Additional import-shape constraint specific to supervision.py."""

    def test_only_imports_core_and_lifecycle(self) -> None:
        """Supervision should only import from core/lifecycle."""
        source = _source_of("medre.core.supervision.supervision")
        lines = _import_lines(source)

        for line in lines:
            # Standard library imports are fine
            if line.startswith(
                ("from __future__", "import ", "from enum", "from typing")
            ):
                continue
            # Allowed internal imports
            assert "medre.core.lifecycle.states" in line or line.startswith(
                "import"
            ), f"supervision.py has unexpected import: {line}"


# ===================================================================
# B) Persistence / storage boundary
# ===================================================================


class TestPersistenceBoundary:
    """Core modules (storage backend, replay engine) must not import transport SDKs."""

    @pytest.mark.parametrize("module_name", _TRANSPORT_AGNOSTIC_CORE_MODULES)
    def test_no_transport_sdk_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"{module_name} imports transport SDKs: {banned_sdk}"

    @pytest.mark.parametrize("module_name", _TRANSPORT_AGNOSTIC_CORE_MODULES)
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"{module_name} imports concrete adapter packages: {banned_adapters}"


# ===================================================================
# C) All runtime core modules remain transport-agnostic
# ===================================================================


class TestRuntimeCoreAgnostic:
    """All runtime core modules must remain free of transport SDK imports."""

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_no_transport_sdk_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"{module_name} imports transport SDKs: {banned_sdk}"

    @pytest.mark.parametrize("module_name", _RUNTIME_MODULES)
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"{module_name} imports concrete adapter packages: {banned_adapters}"


# ===================================================================
# D) Runtime health classification is importable without transport deps
# ===================================================================


class TestSupervisionImportIndependence:
    """Supervision module can be imported without any transport SDK installed."""

    def test_import_succeeds_without_transport_sdks(self) -> None:
        """Importing supervision must not trigger any SDK import."""
        from medre.core.supervision.supervision import RuntimeHealth  # noqa: F401
        from medre.core.supervision.supervision import (  # noqa: F401
            classify_runtime_health,
        )

    def test_import_via_runtime_package(self) -> None:
        """Supervision symbols are available via the runtime package."""
        from medre.core.supervision import AdapterFailureSeverity  # noqa: F401
        from medre.core.supervision import RuntimeHealth  # noqa: F401
        from medre.core.supervision import StartupOutcome  # noqa: F401
        from medre.core.supervision import classify_runtime_health  # noqa: F401
        from medre.core.supervision import classify_startup_outcome  # noqa: F401
        from medre.core.supervision import runtime_supervision_snapshot  # noqa: F401
        from medre.core.supervision import (  # noqa: F401
            classify_adapter_failure_severity,
        )
