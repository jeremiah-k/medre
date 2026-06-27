"""Runtime durability architectural boundary tests.

These tests enforce structural invariants (PC non-negotiables) of the MEDRE
framework via **AST/text import inspection** — not runtime importing of
optional SDKs.  They verify that:

1. Diagnostics modules do not import transport SDKs.
2. Snapshot module does not directly import concrete adapter packages.
3. Storage modules do not import runtime internals.
4. Replay module does not own adapter lifecycle.
5. Durability helpers are transport-agnostic.
6. Soak harness files are fake-only.
7. CLI does not directly instantiate transport SDKs.

Pattern
-------
All tests use source-level text inspection (``importlib`` → read file → scan
import lines).  This avoids triggering SDK imports at test collection time
and works in environments where some or all SDKs are not installed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.architecture_report import _SDK_PACKAGES
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

_RUNTIME_PREFIXES = (
    "medre.runtime.app",
    "medre.runtime.builder",
    "medre.runtime.route_engine",
    "medre.config.routes",
    "medre.runtime.snapshot",
    "medre.runtime.boot_summary",
    "medre.runtime.observability",
    "medre.runtime.errors",
)
"""Runtime module prefixes that storage should not depend on."""

_ADAPTER_LIFECYCLE_PREFIXES = (
    "medre.core.contracts.adapter",
    "medre.core.lifecycle",
)
"""Adapter lifecycle modules that replay should not own."""

_ADAPTER_FACTORIES = (
    "medre.adapters.matrix.",
    "medre.adapters.meshtastic.",
    "medre.adapters.meshcore.",
    "medre.adapters.lxmf.",
)
"""Concrete adapter factory/module prefixes."""


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
# 1. Diagnostics modules no transport SDK imports
# ===================================================================


class TestDiagnosticsNoTransportSdk:
    """Diagnostics modules must not import any transport SDK."""

    @pytest.mark.parametrize(
        "module_name",
        [
            "medre.core.diagnostics",
            "medre.core.diagnostics.replay_metrics",
            "medre.core.diagnostics.snapshot",
            "medre.core.supervision.diagnostics",
            "medre.core.supervision.health",
        ],
    )
    def test_no_sdk_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"{module_name} imports transport SDKs: {banned}"

    @pytest.mark.parametrize(
        "module_name",
        [
            "medre.core.diagnostics",
            "medre.core.diagnostics.replay_metrics",
            "medre.core.diagnostics.snapshot",
            "medre.core.supervision.diagnostics",
        ],
    )
    def test_no_concrete_adapter_imports(self, module_name: str) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"{module_name} imports concrete adapter packages: {banned}"


# ===================================================================
# 2. Snapshot module no direct adapter imports
# ===================================================================


class TestSnapshotNoDirectAdapterImport:
    """runtime/snapshot.py must not directly import concrete adapter packages.

    It may import from ``medre.core.contracts.adapter`` (protocol types) but must
    not import from ``medre.adapters.matrix``, ``medre.adapters.meshtastic``,
    etc.
    """

    def test_snapshot_no_concrete_adapters(self) -> None:
        source = _source_of("medre.runtime.snapshot")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"runtime/snapshot.py imports concrete adapter packages: {banned}"

    def test_snapshot_no_transport_sdks(self) -> None:
        source = _source_of("medre.runtime.snapshot")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"runtime/snapshot.py imports transport SDKs: {banned}"


# ===================================================================
# 3. Storage modules no runtime internals
# ===================================================================


class TestStorageNoRuntimeInternals:
    """Core storage modules must not import runtime internals.

    Storage is a low-level layer; it should not depend on the runtime
    layer (app, builder, route_engine, etc.).

    Note: ``medre.core.engine.replay`` imports ``CapacityController``
    for replay throttling — this is an intentional, narrow dependency
    on the concurrency primitive, not a full runtime coupling.  It is
    tested separately in :class:`TestReplayCapacityDependency`.
    """

    # Pure storage modules (no runtime coupling).
    _PURE_STORAGE_MODULES = [
        "medre.core.storage",
        "medre.core.storage.backend",
        "medre.core.storage.sqlite",
    ]

    @pytest.mark.parametrize(
        "module_name",
        _PURE_STORAGE_MODULES,
    )
    def test_pure_storage_no_runtime_imports(
        self,
        module_name: str,
    ) -> None:
        source = _source_of(module_name)
        lines = _import_lines(source)

        banned = _banned_imports(lines, _RUNTIME_PREFIXES)
        assert banned == [], f"{module_name} imports runtime internals: {banned}"

    def test_replay_only_capacity_import(self) -> None:
        """replay engine may only import CapacityController from runtime.

        It must not import runtime.app, builder, route_engine, etc.
        """
        source = _source_of("medre.core.engine.replay.engine")
        lines = _import_lines(source)

        # Check classic runtime prefixes (app, builder, route_engine, etc.).
        runtime_imports = _banned_imports(lines, _RUNTIME_PREFIXES)
        assert (
            runtime_imports == []
        ), f"replay engine imports banned runtime modules: {runtime_imports}"

        # Also check medre.core.supervision.* — both TYPE_CHECKING-guarded
        # imports (accounting, capacity) are allowed; any other would be banned.
        core_runtime_imports = _banned_imports(lines, ("medre.core.supervision",))
        allowed = [
            "from medre.core.supervision.capacity import CapacityController",
            "from medre.core.supervision.accounting import RuntimeAccounting",
        ]
        disallowed = [line for line in core_runtime_imports if line not in allowed]
        assert (
            disallowed == []
        ), f"replay engine imports disallowed medre.core.supervision modules: {disallowed}"


# ===================================================================
# 4. Replay no adapter lifecycle ownership
# ===================================================================


class TestReplayNoAdapterLifecycleOwnership:
    """Replay module must not own adapter lifecycle.

    Replay operates on stored events and storage backends, not on adapters
    or their lifecycle.  It should not import adapter base or lifecycle
    modules with ownership semantics (start/stop/manage).
    """

    def test_replay_no_adapter_start_stop_patterns(self) -> None:
        """replay engine must not contain adapter lifecycle management patterns."""
        source = _source_of("medre.core.engine.replay.engine")

        # Verify no "start" or "stop" methods on adapters.
        lifecycle_patterns = [
            "adapter.start",
            "adapter.stop",
            ".start()",
            ".stop()",
            "AdapterContract",
            "AdapterContext",
        ]
        violations: list[str] = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            for pattern in lifecycle_patterns:
                if pattern in stripped:
                    violations.append(f"line {i}: {stripped}")

        assert (
            violations == []
        ), "replay engine contains adapter lifecycle patterns:\n" + "\n".join(
            violations
        )

    def test_replay_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.core.engine.replay.engine")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned == []
        ), f"replay engine imports concrete adapter packages: {banned}"


# ===================================================================
# 5. Durability helpers transport-agnostic
# ===================================================================


class TestDurabilityHelpersTransportAgnostic:
    """Durability test files must not import transport SDKs or adapters."""

    _DURABILITY_TEST_FILES = [
        "test_replay_routing_durability.py",
        "test_storage_durability.py",
    ]

    _BANNED_PATTERNS = (
        "from medre.adapters.matrix",
        "from medre.adapters.meshtastic",
        "from medre.adapters.meshcore",
        "from medre.adapters.lxmf",
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

    @pytest.fixture(
        params=_DURABILITY_TEST_FILES,
        ids=_DURABILITY_TEST_FILES,
    )
    def durability_file(self, request: Any) -> Path:
        """Parametrized fixture for each durability test file."""
        tests_dir = Path(__file__).parent
        path = tests_dir / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_durability_files_no_transport_imports(
        self,
        durability_file: Path,
    ) -> None:
        source = _file_source(durability_file)
        lines = source.splitlines()

        violations: list[str] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in self._BANNED_PATTERNS:
                if pattern in stripped:
                    violations.append(f"{durability_file.name}:{i}: {stripped}")
                    break

        assert (
            violations == []
        ), "Durability test files contain transport imports:\n" + "\n".join(violations)


# ===================================================================
# 6. Soak harness fake-only
# ===================================================================


class TestSoakFakeOnly:
    """Soak harness files must only use fake adapters — no live transports."""

    _SOAK_TEST_FILES = [
        "test_soak_harness.py",
        "test_soak_config_builder.py",
        "test_soak_foundations_v2.py",
    ]

    _BANNED_PATTERNS = (
        "from medre.adapters.matrix",
        "from medre.adapters.meshtastic",
        "from medre.adapters.meshcore",
        "from medre.adapters.lxmf",
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

    @pytest.fixture(
        params=_SOAK_TEST_FILES,
        ids=_SOAK_TEST_FILES,
    )
    def soak_file(self, request: Any) -> Path:
        """Parametrized fixture for each soak test file."""
        tests_dir = Path(__file__).parent
        path = tests_dir / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_soak_files_fake_only(self, soak_file: Path) -> None:
        source = _file_source(soak_file)
        lines = source.splitlines()

        violations: list[str] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern in self._BANNED_PATTERNS:
                if pattern in stripped:
                    violations.append(f"{soak_file.name}:{i}: {stripped}")
                    break

        assert (
            violations == []
        ), "Soak test files contain live transport imports:\n" + "\n".join(violations)


# ===================================================================
# 7. CLI no direct transport SDK instantiation
# ===================================================================


class TestCliNoDirectTransportSdk:
    """CLI module must not directly instantiate transport SDKs.

    The CLI may use ``importlib.import_module`` to check SDK availability
    (dynamic probing), but must never ``import nio``, ``import meshtastic``,
    etc. at module top level.
    """

    def test_cli_no_top_level_sdk_imports(self) -> None:
        source = _source_of("medre.cli")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _SDK_PACKAGES)
        assert banned == [], f"cli.py has top-level transport SDK imports: {banned}"

    def test_cli_no_concrete_adapter_imports(self) -> None:
        source = _source_of("medre.cli")
        lines = _import_lines(source)

        banned = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned == [], f"cli.py imports concrete adapter packages: {banned}"

    def test_cli_sdk_probe_is_dynamic_only(self) -> None:
        """SDK availability checks in CLI must use importlib.import_module."""
        source = _source_of("medre.cli.transports")

        # Verify the TRANSPORTS list uses importlib.import_module.
        assert (
            "importlib.import_module" in source
        ), "CLI transports module should use importlib.import_module for SDK probing"

        # Verify no direct SDK instantiation patterns in function bodies
        # (outside of the dynamic probing block).
        direct_instantiation_patterns = (
            "nio.AsyncClient(",
            "MeshtasticClient(",
            "MeshCore(",
            "RNS.Reticulum(",
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
        ), "transports.py directly instantiates transport SDKs:\n" + "\n".join(
            violations
        )
