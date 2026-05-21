"""Boundary/regression tests protecting architectural invariants around resource
controls, shutdown, and runtime modules.

These tests verify that resource-control code (CapacityController, runtime
shutdown) remains isolated from concrete transport SDKs and adapter
implementations, and that the reverse dependency also holds — adapters and
sessions never import resource-control modules.

TRACK 9 — Resource Boundary Tests
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared constants & helpers
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parent.parent / "src"

_SDK_PACKAGES = (
    "nio",
    "meshtastic",
    "meshcore",
    "RNS",
    "lxmf",
    "LXMF",
    "aiohttp",
    "serial",
    "serial_asyncio",
)
"""Third-party transport SDK package names as they appear in import statements."""

_ADAPTER_PREFIXES = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)
"""Concrete adapter package prefixes (excludes medre.core.contracts.adapter and fake_*)."""

_RESOURCE_CONTROL_MODULES = ("medre.core.runtime.capacity",)
"""Runtime resource-control modules that must stay transport-agnostic."""


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


def _all_py_files_under(directory: Path, prefix: str) -> list[tuple[str, str]]:
    """Yield (module_name, source) for every .py file under *directory*."""
    results: list[tuple[str, str]] = []
    for py in sorted(directory.rglob("*.py")):
        rel = py.relative_to(_SRC)
        parts = list(rel.with_suffix("").parts)
        module_name = ".".join(parts)
        with open(py) as f:
            source = f.read()
        results.append((module_name, source))
    return results


# ===================================================================
# Test 1: CapacityController does not import transport SDKs
# ===================================================================


class TestCapacityControllerSDKIsolation:
    """CapacityController must stay free of transport SDK imports."""

    def test_capacity_controller_does_not_import_transport_sdks(self) -> None:
        """Verify capacity.py source has zero references to transport SDK packages."""
        source = _source_of("medre.core.runtime.capacity")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert (
            banned_sdk == []
        ), f"CapacityController imports transport SDKs: {banned_sdk}"

        # Also verify no reference in the full source text (catches string refs)
        for sdk in _SDK_PACKAGES:
            assert sdk not in source, f"capacity.py mentions SDK '{sdk}' in source text"


# ===================================================================
# Test 2: Runtime resource controls do not import adapters directly
# ===================================================================


class TestResourceControlAdapterIsolation:
    """Resource control modules must not import concrete adapter classes."""

    def test_runtime_resource_controls_do_not_import_adapters_directly(self) -> None:
        """Check capacity.py and shutdown code in app.py for adapter imports."""
        violations: list[str] = []

        # Check capacity.py
        cap_source = _source_of("medre.core.runtime.capacity")
        cap_lines = _import_lines(cap_source)
        cap_banned = _banned_imports(cap_lines, _ADAPTER_PREFIXES)
        for line in cap_banned:
            violations.append(f"capacity.py: {line}")

        # Check app.py — only non-TYPE_CHECKING imports (runtime imports
        # adapters only behind TYPE_CHECKING for annotations)
        app_source = _source_of("medre.runtime.app")
        in_type_checking = False
        for raw_line in app_source.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith("if TYPE_CHECKING"):
                in_type_checking = True
                continue
            if in_type_checking and stripped == "":
                in_type_checking = False
                continue
            if in_type_checking:
                continue
            # Only check actual import lines outside TYPE_CHECKING
            if not stripped.startswith(("import ", "from ")):
                continue
            for prefix in _ADAPTER_PREFIXES:
                if prefix in stripped:
                    violations.append(f"app.py (runtime): {stripped}")
                    break

        assert (
            violations == []
        ), "Resource-control modules import adapters directly:\n" + "\n".join(
            violations
        )


# ===================================================================
# Test 3: Adapters do not import resource controls
# ===================================================================


class TestAdapterResourceControlIsolation:
    """Adapter modules must not import resource-control or runtime limit modules."""

    def test_adapters_do_not_import_resource_controls(self) -> None:
        """Check that no adapter module imports from medre.core.runtime.capacity."""
        adapters_dir = _SRC / "medre" / "adapters"
        violations: list[str] = []

        for module_name, source in _all_py_files_under(adapters_dir, "medre.adapters"):
            lines = _import_lines(source)
            banned = _banned_imports(lines, _RESOURCE_CONTROL_MODULES)
            for line in banned:
                violations.append(f"{module_name}: {line}")

        assert (
            violations == []
        ), "Adapter modules import resource controls:\n" + "\n".join(violations)


# ===================================================================
# Test 4: Sessions do not know about resource controls
# ===================================================================


class TestSessionResourceControlIsolation:
    """Session modules must not import capacity controllers or runtime limits."""

    def test_sessions_do_not_know_about_resource_controls(self) -> None:
        """Check that session.py files under adapters/ do not import resource controls."""
        adapters_dir = _SRC / "medre" / "adapters"
        violations: list[str] = []

        runtime_imports = (
            "medre.core.runtime.capacity",
            "medre.runtime.app",
            "CapacityController",
            "RuntimeLimits",
        )

        for module_name, source in _all_py_files_under(adapters_dir, "medre.adapters"):
            # Only check session.py files
            if not module_name.endswith(".session") and "/session" not in module_name:
                continue
            lines = _import_lines(source)
            banned = _banned_imports(lines, runtime_imports)
            for line in banned:
                violations.append(f"{module_name}: {line}")

        assert (
            violations == []
        ), "Session modules import resource controls:\n" + "\n".join(violations)


# ===================================================================
# Test 5: ReplayEngine remains SDK-free
# ===================================================================


class TestReplayEngineSDKFreedom:
    """ReplayEngine must not import transport SDKs (re-affirm boundary)."""

    def test_replay_engine_remains_sdk_free(self) -> None:
        """Verify replay.py has zero transport SDK imports."""
        source = _source_of("medre.core.storage.replay")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"ReplayEngine imports transport SDKs: {banned_sdk}"

        # Also check for concrete adapter package imports
        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"ReplayEngine imports concrete adapter packages: {banned_adapters}"


# ===================================================================
# Test 6: RouteEngine remains SDK-free
# ===================================================================


class TestRouteEngineSDKFreedom:
    """RouteEngine must not import transport SDKs."""

    def test_route_engine_remains_sdk_free(self) -> None:
        """Verify route_engine.py has zero transport SDK imports."""
        source = _source_of("medre.runtime.route_engine")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"RouteEngine imports transport SDKs: {banned_sdk}"

        # Also check for concrete adapter package imports
        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"RouteEngine imports concrete adapter packages: {banned_adapters}"
