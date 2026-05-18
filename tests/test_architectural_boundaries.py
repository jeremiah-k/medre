"""Architectural boundary and regression tests.

These tests protect structural invariants of the MEDRE framework — ensuring
that core/routing/replay/pipeline modules stay transport-SDK-free, that the
RuntimeBuilder can be imported and exercised with fake adapters, that
no replay test file pulls in live transport dependencies, and that the
canonical import architecture is enforced.

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

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
"""Concrete adapter package prefixes (excludes medre.core.contracts.adapter and fake_*)."""


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


def _scan_dir_for_prefixes(
    root: Path, prefixes: tuple[str, ...]
) -> list[str]:
    """Scan all .py files under *root* for lines starting with any prefix.

    Returns a list of ``"relative_path:line_no: line"`` strings.
    Skips blank lines and comments.
    """
    assert root.exists(), f"missing directory: {root}"
    files = sorted(root.rglob("*.py"))
    assert files, f"no Python files scanned under {root}"
    violations: list[str] = []
    for p in files:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if any(s.startswith(prefix) for prefix in prefixes):
                violations.append(f"{p}:{i}: {s}")
    return violations


def _scan_multiple_dirs_for_prefixes(
    roots: tuple[Path, ...], prefixes: tuple[str, ...]
) -> list[str]:
    """Scan multiple directories, collecting violations."""
    violations: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        violations.extend(_scan_dir_for_prefixes(root, prefixes))
    return violations


# ===================================================================
# A) ReplayEngine does not import transport SDKs
# ===================================================================


class TestReplayEngineBoundary:
    """ReplayEngine (src/medre/core/storage/replay.py) must not import
    any concrete transport SDK or concrete adapter package."""

    def test_replay_engine_does_not_import_transport_sdks(self) -> None:
        source = _source_of("medre.core.storage.replay")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"replay.py imports transport SDKs: {banned_sdk}"
        )

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"replay.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# B) RouteEngine does not import transport SDKs
# ===================================================================


class TestRouteEngineBoundary:
    """RouteEngine (src/medre/runtime/route_engine.py) must not import
    any concrete transport SDK or concrete adapter package."""

    def test_route_engine_does_not_import_transport_sdks(self) -> None:
        source = _source_of("medre.runtime.route_engine")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"route_engine.py imports transport SDKs: {banned_sdk}"
        )

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"route_engine.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# C) PipelineRunner does not import concrete SDKs
# ===================================================================


class TestPipelineRunnerBoundary:
    """PipelineRunner (src/medre/core/engine/pipeline.py) must not import
    any concrete transport SDK or concrete adapter package.
    Adapter contract types live in medre.core.contracts.adapter — core
    must not import medre.adapters at runtime."""

    def test_pipeline_runner_does_not_import_concrete_sdks(self) -> None:
        source = _source_of("medre.core.engine.pipeline")
        lines = _import_lines(source)

        banned_sdk = _banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], (
            f"pipeline.py imports transport SDKs: {banned_sdk}"
        )

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert banned_adapters == [], (
            f"pipeline.py imports concrete adapter packages: {banned_adapters}"
        )


# ===================================================================
# D) RuntimeBuilder imports without live deps
# ===================================================================


class TestRuntimeBuilderImportBoundary:
    """RuntimeBuilder import must succeed regardless of whether live
    transport SDKs are installed."""

    def test_runtime_builder_imports_without_live_deps(self) -> None:
        # If import succeeds, the boundary holds.  In environments where
        # some SDKs happen to be installed (dev machines), we verify the
        # import does not fail.  The builder uses lazy _AdapterFactory
        # descriptors, so importing builder.py never triggers SDK imports.
        mod = importlib.import_module("medre.runtime.builder")
        assert hasattr(mod, "RuntimeBuilder"), (
            "RuntimeBuilder not found in medre.runtime.builder"
        )


# ===================================================================
# E) Embedding example / RuntimeBuilder with fake adapters
# ===================================================================


class TestFakeAdapterRuntimeBuild:
    """RuntimeBuilder can build a runtime using only fake adapters — no
    live SDKs required.  This verifies the embedding path works without
    optional deps."""

    @pytest.fixture
    async def _temp_storage(self) -> AsyncGenerator:
        """Temp SQLite storage for the pipeline build test."""
        from medre.core.storage.sqlite import SQLiteStorage

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        storage = SQLiteStorage(db_path=db_path)
        await storage.initialize()
        yield storage
        await storage.close()
        os.unlink(db_path)

    async def test_build_with_fake_adapters_no_live_sdks(
        self, _temp_storage: Any
    ) -> None:
        from medre.adapters.fake_matrix import FakeMatrixAdapter
        from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter

        from medre.core.events.bus import EventBus
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.observability.metrics import Diagnostician
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner

        # Construct a minimal pipeline using fake adapters only — no SDK calls.
        event_bus = EventBus()
        rendering_pipeline = RenderingPipeline()
        rendering_pipeline.register(TextRenderer(), priority=100)
        router = Router()
        fallback_resolver = FallbackResolver()
        relation_resolver = RelationResolver(storage=_temp_storage)
        diagnostician = Diagnostician()
        route_stats = RouteStats()

        fake_matrix = FakeMatrixAdapter(adapter_id="fake_bot1")
        fake_mesh = FakeMeshtasticAdapter()

        pipeline_config = PipelineConfig(
            storage=_temp_storage,
            event_bus=event_bus,
            rendering_pipeline=rendering_pipeline,
            router=router,
            fallback_resolver=fallback_resolver,
            relation_resolver=relation_resolver,
            diagnostician=diagnostician,
            route_stats=route_stats,
            adapters={"fake_bot1": fake_matrix, "fake_radio": fake_mesh},
        )

        runner = PipelineRunner(config=pipeline_config)
        assert runner is not None
        # Verify adapters are registered
        assert "fake_bot1" in pipeline_config.adapters
        assert "fake_radio" in pipeline_config.adapters


# ===================================================================
# F) No replay tests require live transports
# ===================================================================


class TestReplayTestPurity:
    """Scan replay test files for live transport import patterns — assert
    none found.  Replay tests should use fake adapters and the in-memory
    storage backend exclusively."""

    # Banned import patterns in replay test files
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
        params=[
            "test_replay.py",
            "test_replay_routing.py",
            "test_replay_summary.py",
        ]
    )
    def replay_test_file(self, request: Any) -> Path:
        """Parametrized fixture for each replay test file."""
        tests_dir = Path(__file__).parent
        path = tests_dir / request.param
        if not path.exists():
            pytest.skip(f"{request.param} not found")
        return path

    def test_no_replay_tests_require_live_transports(
        self, replay_test_file: Path
    ) -> None:
        source = replay_test_file.read_text()
        lines = source.splitlines()

        violations: list[str] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments
            if stripped.startswith("#"):
                continue
            for pattern in self._BANNED_PATTERNS:
                if pattern in stripped:
                    violations.append(
                        f"{replay_test_file.name}:{i}: {stripped}"
                    )
                    break

        assert violations == [], (
            f"Replay test files contain live transport imports:\n"
            + "\n".join(violations)
        )


# ===================================================================
# G) Core → adapters import boundary
# ===================================================================


class TestCoreDoesNotImportAdapters:
    """Core modules must not have runtime imports from medre.adapters.

    Adapter contract types live in medre.core.contracts.adapter.
    Core must not depend on adapters or config.
    """

    def test_no_runtime_core_to_adapters_imports(self) -> None:
        """Scan all core .py files for medre.adapters imports."""
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.adapters") or stripped.startswith("import medre.adapters"):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert violations == [], (
            f"Core modules must not import from medre.adapters:\n"
            + "\n".join(violations)
        )


# ===================================================================
# H) Config → adapters import boundary
# ===================================================================


class TestConfigDoesNotImportAdapters:
    """Config modules must not import concrete adapter packages.

    Adapter config models live in medre.config.adapters.*, not in
    medre.adapters.*.config.  The config/adapters/ subpackage must
    also not import from medre.adapters (credential helpers are
    config-owned).
    """

    def test_no_config_to_adapters_imports(self) -> None:
        """Scan ALL config .py files for medre.adapters imports."""
        repo_root = Path(__file__).resolve().parents[1]
        config_dir = repo_root / "src" / "medre" / "config"
        assert config_dir.exists(), f"config directory not found: {config_dir}"

        violations: list[str] = []
        for py_file in sorted(config_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.adapters") or stripped.startswith("import medre.adapters"):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert violations == [], (
            f"Config modules must not import from medre.adapters:\n"
            + "\n".join(violations)
        )


# ===================================================================
# I) Core does not import config
# ===================================================================


class TestCoreDoesNotImportConfig:
    """Core modules must not import from medre.config.

    Core is the innermost layer and must not depend on config.
    """

    def test_no_core_to_config_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("from medre.config") or stripped.startswith("import medre.config"):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert violations == [], (
            f"Core modules must not import from medre.config:\n"
            + "\n".join(violations)
        )


# ===================================================================
# J) No old/noncanonical imports remain in src or tests
# ===================================================================


class TestNoOldImports:
    """No source or test file may import from old/noncanonical modules.

    Enforces that the following old modules are not referenced:
    - medre.adapters.base
    - medre.core.ports
    - medre.core.adapter_base
    - medre.adapters.*.config (config dataclasses live in medre.config.adapters.*)
    - ConfigError classes from medre.adapters.*.errors
    """

    # Forbidden import prefixes — these old modules must not be imported.
    _FORBIDDEN_PREFIXES = (
        "from medre.adapters.base",
        "import medre.adapters.base",
        "from medre.core.ports",
        "import medre.core.ports",
        "from medre.core.adapter_base",
        "import medre.core.adapter_base",
        "from medre.adapters.matrix.config",
        "from medre.adapters.meshtastic.config",
        "from medre.adapters.meshcore.config",
        "from medre.adapters.lxmf.config",
        "import medre.adapters.matrix.config",
        "import medre.adapters.meshtastic.config",
        "import medre.adapters.meshcore.config",
        "import medre.adapters.lxmf.config",
        "from medre.adapters.matrix.errors import MatrixConfigError",
        "from medre.adapters.meshtastic.errors import MeshtasticConfigError",
        "from medre.adapters.meshcore.errors import MeshCoreConfigError",
        "from medre.adapters.lxmf.errors import LxmfConfigError",
    )

    def test_no_old_imports_in_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_dir = repo_root / "src"
        assert src_dir.exists()

        violations = _scan_dir_for_prefixes(src_dir, self._FORBIDDEN_PREFIXES)
        assert violations == [], (
            f"Old/noncanonical imports found in src/:\n"
            + "\n".join(violations)
        )

    def test_no_old_imports_in_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tests_dir = repo_root / "tests"
        assert tests_dir.exists()

        violations = _scan_dir_for_prefixes(tests_dir, self._FORBIDDEN_PREFIXES)
        assert violations == [], (
            f"Old/noncanonical imports found in tests/:\n"
            + "\n".join(violations)
        )


# ===================================================================
# K) No BaseAdapter references in source or tests
# ===================================================================


class TestNoOldAdapterBaseReferences:
    """No source or test file should reference the old adapter base class name.

    The old name has been renamed to AdapterContract.  All source and
    test code should use AdapterContract instead.
    """

    # Use string concat to avoid the literal appearing in this test file.
    _OLD_NAME = "Base" + "Adapter"

    def test_no_baseadapter_in_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        src_dir = repo_root / "src"
        assert src_dir.exists()

        violations: list[str] = []
        old_name = self._OLD_NAME
        for py_file in sorted(src_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if old_name in stripped:
                    violations.append(f"{py_file.relative_to(repo_root)}:{i}: {stripped}")

        assert violations == [], (
            f"{old_name} references found in src/:\n"
            + "\n".join(violations)
        )

    def test_no_baseadapter_in_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tests_dir = repo_root / "tests"
        assert tests_dir.exists()

        violations: list[str] = []
        old_name = self._OLD_NAME
        for py_file in sorted(tests_dir.rglob("*.py")):
            # This test file itself references the old name in its scan
            # logic — exclude it from the scan.
            if py_file.name == "test_architectural_boundaries.py":
                continue
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if old_name in stripped:
                    violations.append(f"{py_file.relative_to(repo_root)}:{i}: {stripped}")

        assert violations == [], (
            f"{old_name} references found in tests/:\n"
            + "\n".join(violations)
        )


# ===================================================================
# L) Old/noncanonical module files do not exist
# ===================================================================


class TestOldModulesRemoved:
    """Old/noncanonical module files must not exist on disk."""

    _EXPECTED_ABSENT = [
        "src/medre/core/ports.py",
        "src/medre/core/adapter_base.py",
        "src/medre/adapters/base.py",
        "src/medre/adapters/matrix/config.py",
        "src/medre/adapters/meshtastic/config.py",
        "src/medre/adapters/meshcore/config.py",
        "src/medre/adapters/lxmf/config.py",
    ]

    def test_old_modules_removed(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        remaining = [p for p in self._EXPECTED_ABSENT if (repo_root / p).exists()]
        assert remaining == [], (
            f"Old/noncanonical modules still exist:\n"
            + "\n".join(remaining)
        )


# ===================================================================
# M) Canonical core contract exports
# ===================================================================


class TestCanonicalContractExports:
    """Verify that medre.core.contracts.adapter exports the expected
    canonical names.
    """

    _EXPECTED_NAMES = [
        "AdapterContract",
        "AdapterRole",
        "AdapterCodec",
        "AdapterContext",
        "AdapterCapabilities",
        "AdapterInfo",
        "AdapterDeliveryResult",
        "AdapterSendError",
        "AdapterPermanentError",
    ]

    def test_adapter_module_exports(self) -> None:
        import medre.core.contracts.adapter as mod

        for name in self._EXPECTED_NAMES:
            assert hasattr(mod, name), (
                f"medre.core.contracts.adapter missing export: {name}"
            )

    def test_contracts_init_reexports(self) -> None:
        import medre.core.contracts as pkg

        for name in self._EXPECTED_NAMES:
            assert hasattr(pkg, name), (
                f"medre.core.contracts missing re-export: {name}"
            )


# ===================================================================
# N) Canonical config error hierarchy
# ===================================================================


class TestConfigErrorHierarchy:
    """Config errors must be ValueError subclasses, not adapter runtime
    errors.
    """

    def test_matrix_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MatrixConfigError,
        )

        assert issubclass(MatrixConfigError, AdapterConfigError)
        assert issubclass(MatrixConfigError, ValueError)

    def test_meshtastic_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MeshtasticConfigError,
        )

        assert issubclass(MeshtasticConfigError, AdapterConfigError)
        assert issubclass(MeshtasticConfigError, ValueError)

    def test_meshcore_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            MeshCoreConfigError,
        )

        assert issubclass(MeshCoreConfigError, AdapterConfigError)
        assert issubclass(MeshCoreConfigError, ValueError)

    def test_lxmf_config_error_hierarchy(self) -> None:
        from medre.config.adapters.errors import (
            AdapterConfigError,
            LxmfConfigError,
        )

        assert issubclass(LxmfConfigError, AdapterConfigError)
        assert issubclass(LxmfConfigError, ValueError)


# ===================================================================
# O) Config errors are not adapter runtime errors
# ===================================================================


class TestConfigErrorsNotRuntimeErrors:
    """Config errors must not be subclasses of adapter runtime errors."""

    def test_matrix_config_error_not_runtime_error(self) -> None:
        from medre.adapters.matrix.errors import MatrixError
        from medre.config.adapters.errors import MatrixConfigError

        assert not issubclass(MatrixConfigError, MatrixError)

    def test_meshtastic_config_error_not_runtime_error(self) -> None:
        from medre.adapters.meshtastic.errors import MeshtasticError
        from medre.config.adapters.errors import MeshtasticConfigError

        assert not issubclass(MeshtasticConfigError, MeshtasticError)

    def test_meshcore_config_error_not_runtime_error(self) -> None:
        from medre.adapters.meshcore.errors import MeshCoreError
        from medre.config.adapters.errors import MeshCoreConfigError

        assert not issubclass(MeshCoreConfigError, MeshCoreError)

    def test_lxmf_config_error_not_runtime_error(self) -> None:
        from medre.adapters.lxmf.errors import LxmfError
        from medre.config.adapters.errors import LxmfConfigError

        assert not issubclass(LxmfConfigError, LxmfError)


# ===================================================================
# P) Matrix credential sidecar behavior
# ===================================================================


class TestMatrixCredentialSidecar:
    """Verify that Matrix credential sidecar helpers live in the config
    layer and preserve expected behavior.
    """

    def test_get_credentials_path_respects_xdg(self) -> None:
        import os
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import get_credentials_path

        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/config"}):
            path = get_credentials_path()
            assert str(path).startswith("/custom/config/")

    def test_get_credentials_path_default(self) -> None:
        import os
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import get_credentials_path

        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            path = get_credentials_path()
            assert ".config" in str(path)
            assert "medre/credentials/matrix.json" in str(path)

    def test_load_credentials_missing_file(self) -> None:
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path"
        ) as mock_path:
            from pathlib import Path

            mock_path.return_value = Path("/nonexistent/medre/credentials/matrix.json")
            result = load_credentials_json()
            assert result is None

    def test_load_credentials_valid_json(self, tmp_path: Any) -> None:
        import json
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        cred_file = tmp_path / "matrix.json"
        cred_file.write_text(
            json.dumps({
                "homeserver": "https://matrix.org",
                "user_id": "@bot:matrix.org",
                "access_token": "syt_abc123",
            })
        )

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path",
            return_value=cred_file,
        ):
            result = load_credentials_json()
            assert result is not None
            assert result["homeserver"] == "https://matrix.org"
            assert result["user_id"] == "@bot:matrix.org"

    def test_load_credentials_invalid_json(self, tmp_path: Any) -> None:
        from unittest.mock import patch

        from medre.config.adapters.matrix_credentials import load_credentials_json

        cred_file = tmp_path / "matrix.json"
        cred_file.write_text("not valid json{{{")

        with patch(
            "medre.config.adapters.matrix_credentials.get_credentials_path",
            return_value=cred_file,
        ):
            result = load_credentials_json()
            assert result is None


# ===================================================================
# Q) ConfigError import from canonical locations
# ===================================================================


class TestConfigErrorCanonicalImports:
    """ConfigError classes must be imported from medre.config.adapters.errors."""

    # These are allowed paths for importing ConfigError classes.
    _ALLOWED_ERROR_PATHS = (
        "from medre.config.adapters.errors import ",
    )

    # These are FORBIDDEN — ConfigError classes must not be imported
    # from config dataclass modules.
    _FORBIDDEN_ERROR_IMPORTS = (
        "from medre.config.adapters.matrix import MatrixConfigError",
        "from medre.config.adapters.meshtastic import MeshtasticConfigError",
        "from medre.config.adapters.meshcore import MeshCoreConfigError",
        "from medre.config.adapters.lxmf import LxmfConfigError",
    )

    def test_config_errors_not_imported_from_dataclass_modules(self) -> None:
        """No source or test file should import ConfigError from dataclass modules."""
        repo_root = Path(__file__).resolve().parents[1]
        violations: list[str] = []
        for py_file in sorted((repo_root / "src").rglob("*.py")):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(stripped.startswith(p) for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(f"{py_file.relative_to(repo_root)}:{i}: {stripped}")
        for py_file in sorted((repo_root / "tests").rglob("*.py")):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(stripped.startswith(p) for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(f"{py_file.relative_to(repo_root)}:{i}: {stripped}")

        assert violations == [], (
            "ConfigError imports from dataclass modules found (must use medre.config.adapters.errors):\n"
            + "\n".join(violations)
        )

    def test_package_re_exports_config_errors(self) -> None:
        """Package-level re-exports of ConfigError classes are valid from medre.config.adapters."""
        from medre.config.adapters.errors import MatrixConfigError
        from medre.config.adapters.errors import LxmfConfigError
        from medre.config.adapters.errors import MeshtasticConfigError
        from medre.config.adapters.errors import MeshCoreConfigError
        _ = MatrixConfigError, LxmfConfigError, MeshtasticConfigError, MeshCoreConfigError
        import medre.config.adapters as mod
        assert hasattr(mod, "MatrixConfigError")
        assert hasattr(mod, "LxmfConfigError")
        assert hasattr(mod, "MeshtasticConfigError")
        assert hasattr(mod, "MeshCoreConfigError")


# ===================================================================
# R) No active stale architecture references in docs
# ===================================================================


class TestNoActiveStaleDocsReferences:
    """No active documentation should reference removed modules as if current.

    Historical documents (explicitly marked as pre-refactor) are exempt.
    docs/ARCHITECTURE_PLAN.md may mention removed modules only in
    "removed/merged" historical context.
    """

    _STALE_PATTERNS = (
        "BaseAdapter",
        "medre.adapters.base",
        "adapters/base.py",
        "medre.core.ports",
        "core/ports.py",
        "medre.core.adapter_base",
        "core/adapter_base.py",
        "medre.adapters.matrix.config",
        "medre.adapters.meshtastic.config",
        "medre.adapters.meshcore.config",
        "medre.adapters.lxmf.config",
        "adapters/matrix/config.py",
        "adapters/meshtastic/config.py",
        "adapters/meshcore/config.py",
        "adapters/lxmf/config.py",
        "medre.adapters.matrix.errors.MatrixConfigError",
        "medre.adapters.meshtastic.errors.MeshtasticConfigError",
        "medre.adapters.meshcore.errors.MeshCoreConfigError",
        "medre.adapters.lxmf.errors.LxmfConfigError",
    )

    _HISTORICAL_CONTEXT_WORDS = (
        "removed",
        "merged",
        "replaced",
        "deleted",
        "superseded",
        "do not exist",
        "must not be imported",
        "pre-refactor",
        "historical",
        "tranche",
        "adaptercontract",
        "renamed",
    )

    def test_no_active_stale_references_in_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        assert docs_dir.exists()

        violations: list[tuple[str, int, str]] = []

        for md_file in sorted(docs_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")

            # Exempt explicitly historical documents
            if md_file.name == "66-release-hygiene-audit.md" and "pre-refactor architecture" in text:
                continue

            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not any(pattern in stripped for pattern in self._STALE_PATTERNS):
                    continue

                # Allow lines with historical-context markers (e.g. "removed",
                # "merged", "replaced", "historical", "tranche", etc.)
                lowered = stripped.lower()
                if any(word in lowered for word in self._HISTORICAL_CONTEXT_WORDS):
                    continue

                violations.append((str(md_file.relative_to(repo_root)), i, stripped))

        assert not violations, (
            "Active stale architecture references found in docs:\n"
            + "\n".join(f"{f}:{l}: {s}" for f, l, s in violations)
        )
