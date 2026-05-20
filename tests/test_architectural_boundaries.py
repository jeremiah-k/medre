"""Architectural boundary and regression tests.

These tests protect structural invariants of the MEDRE framework — ensuring
that core/routing/replay/pipeline modules stay transport-SDK-free, that the
RuntimeBuilder can be imported and exercised with fake adapters, that
no replay test file pulls in live transport dependencies, and that the
canonical import architecture is enforced.

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

import ast as _ast
import importlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from tests.helpers.import_ast import (
    all_imports,
    check_banned_ast,
    collect_imports_from_node,
    runtime_imports,
    top_level_imports,
)

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


def _scan_dir_for_prefixes(root: Path, prefixes: tuple[str, ...]) -> list[str]:
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
        assert banned_sdk == [], f"replay.py imports transport SDKs: {banned_sdk}"

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"replay.py imports concrete adapter packages: {banned_adapters}"


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
        assert banned_sdk == [], f"route_engine.py imports transport SDKs: {banned_sdk}"

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"route_engine.py imports concrete adapter packages: {banned_adapters}"


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
        assert banned_sdk == [], f"pipeline.py imports transport SDKs: {banned_sdk}"

        banned_adapters = _banned_imports(lines, _ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"pipeline.py imports concrete adapter packages: {banned_adapters}"


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
        assert hasattr(
            mod, "RuntimeBuilder"
        ), "RuntimeBuilder not found in medre.runtime.builder"


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
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.observability.metrics import Diagnostician
        from medre.core.planning.fallback_resolution import FallbackResolver
        from medre.core.planning.relation_resolution import RelationResolver
        from medre.core.rendering.renderer import RenderingPipeline
        from medre.core.rendering.text import TextRenderer
        from medre.core.routing.router import Router
        from medre.core.routing.stats import RouteStats

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
                    violations.append(f"{replay_test_file.name}:{i}: {stripped}")
                    break

        assert (
            violations == []
        ), "Replay test files contain live transport imports:\n" + "\n".join(violations)


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
                if stripped.startswith("from medre.adapters") or stripped.startswith(
                    "import medre.adapters"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Core modules must not import from medre.adapters:\n" + "\n".join(violations)


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
                if stripped.startswith("from medre.adapters") or stripped.startswith(
                    "import medre.adapters"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Config modules must not import from medre.adapters:\n" + "\n".join(
            violations
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
                if stripped.startswith("from medre.config") or stripped.startswith(
                    "import medre.config"
                ):
                    rel = py_file.relative_to(repo_root)
                    violations.append(f"{rel}:{i}: {stripped}")

        assert (
            violations == []
        ), "Core modules must not import from medre.config:\n" + "\n".join(violations)


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
        assert (
            violations == []
        ), "Old/noncanonical imports found in src/:\n" + "\n".join(violations)

    def test_no_old_imports_in_tests(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        tests_dir = repo_root / "tests"
        assert tests_dir.exists()

        violations = _scan_dir_for_prefixes(tests_dir, self._FORBIDDEN_PREFIXES)
        assert (
            violations == []
        ), "Old/noncanonical imports found in tests/:\n" + "\n".join(violations)


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
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert violations == [], f"{old_name} references found in src/:\n" + "\n".join(
            violations
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
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert (
            violations == []
        ), f"{old_name} references found in tests/:\n" + "\n".join(violations)


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
        assert remaining == [], "Old/noncanonical modules still exist:\n" + "\n".join(
            remaining
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
            assert hasattr(
                mod, name
            ), f"medre.core.contracts.adapter missing export: {name}"

    def test_contracts_init_reexports(self) -> None:
        import medre.core.contracts as pkg

        for name in self._EXPECTED_NAMES:
            assert hasattr(pkg, name), f"medre.core.contracts missing re-export: {name}"


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
            json.dumps(
                {
                    "homeserver": "https://matrix.org",
                    "user_id": "@bot:matrix.org",
                    "access_token": "syt_abc123",
                }
            )
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
    _ALLOWED_ERROR_PATHS = ("from medre.config.adapters.errors import ",)

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
                if any(p in stripped for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )
        for py_file in sorted((repo_root / "tests").rglob("*.py")):
            # Exclude this test file — it defines the forbidden patterns as literals.
            if py_file.name == "test_architectural_boundaries.py":
                continue
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if any(p in stripped for p in self._FORBIDDEN_ERROR_IMPORTS):
                    violations.append(
                        f"{py_file.relative_to(repo_root)}:{i}: {stripped}"
                    )

        assert violations == [], (
            "ConfigError imports from dataclass modules found (must use medre.config.adapters.errors):\n"
            + "\n".join(violations)
        )

    def test_package_re_exports_config_errors(self) -> None:
        """Package-level re-exports of ConfigError classes are valid from medre.config.adapters."""
        from medre.config.adapters.errors import (
            LxmfConfigError,
            MatrixConfigError,
            MeshCoreConfigError,
            MeshtasticConfigError,
        )

        _ = (
            MatrixConfigError,
            LxmfConfigError,
            MeshtasticConfigError,
            MeshCoreConfigError,
        )
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

    docs/ARCHITECTURE_PLAN.md may mention removed modules only in
    "does not exist" / "must not be imported" factual context.
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

    _ALLOWED_CONTEXT_WORDS = (
        "does not exist",
        "must not be imported",
        "noncanonical",
    )

    def test_no_active_stale_references_in_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        assert docs_dir.exists()

        violations: list[tuple[str, int, str]] = []

        for md_file in sorted(docs_dir.rglob("*.md")):
            text = md_file.read_text(encoding="utf-8")

            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not any(pattern in stripped for pattern in self._STALE_PATTERNS):
                    continue

                # Allow lines with factual-context markers (e.g. "removed",
                # "merged", "replaced", "historical", "tranche", etc.)
                lowered = stripped.lower()
                if any(word in lowered for word in self._ALLOWED_CONTEXT_WORDS):
                    continue

                violations.append((str(md_file.relative_to(repo_root)), i, stripped))

        assert (
            not violations
        ), "Active stale architecture references found in docs:\n" + "\n".join(
            f"{f}:{line}: {s}" for f, line, s in violations
        )


# ===================================================================
# S) No stale transitional wording in documentation
# ===================================================================


class TestNoStaleWordingInDocs:
    """Scan docs/**/*.md for discouraged transitional/historical phrases.

    This test catches phrasing that frames the current architecture as
    transitional, legacy, or historical — rather than as the intended
    architecture from the start.

    Allowed exceptions:
    - Lines containing precise removal statements (e.g. "was replaced by",
      "was removed", "does not exist").
    """

    _FORBIDDEN_PHRASES = (
        "legacy adapter framework",
        "legacy adapter layer",
        "historical architecture",
        "compatibility shim",
        "compatibility layer",
        "pre-refactor architecture",
        "transitional import path",
        "migration-era",
        "old adapter framework",
        "old architecture",
        "backward compatibility layer",
    )

    # Lines containing these phrases are exempt — they are factual
    # noncanonical-module statements, not transitional framing.
    _EXEMPTION_WORDS = (
        "does not exist",
        "must not be imported",
        "noncanonical",
        # Negation context — "no compatibility shims", "not a compatibility layer"
        "no ",
        "no.",
        "not ",
        "not.",
        "without",
        # Module description context — compat.py file tree comments
        "compat.py",
    )

    # These files are fully exempt from the wording check.
    _EXEMPT_FILES: frozenset[str] = frozenset()

    def test_no_stale_wording_in_docs(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        docs_dir = repo_root / "docs"
        assert docs_dir.exists()

        violations: list[tuple[str, int, str]] = []

        for md_file in sorted(docs_dir.rglob("*.md")):
            if md_file.name in self._EXEMPT_FILES:
                continue

            text = md_file.read_text(encoding="utf-8")

            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                lowered = stripped.lower()

                if not any(phrase in lowered for phrase in self._FORBIDDEN_PHRASES):
                    continue

                # Exempt lines that are factual removal/merge statements
                if any(word in lowered for word in self._EXEMPTION_WORDS):
                    continue

                violations.append((str(md_file.relative_to(repo_root)), i, stripped))

        assert (
            not violations
        ), "Stale transitional wording found in docs:\n" + "\n".join(
            f"{f}:{line}: {s}" for f, line, s in violations
        )


# ---------------------------------------------------------------------------
# AST-based import extraction helpers (Sections T–W)
# ---------------------------------------------------------------------------


# Backward-compatible aliases for moved AST helpers.
_collect_imports_from_node = collect_imports_from_node
_top_level_imports = top_level_imports
_all_imports = all_imports
_runtime_imports = runtime_imports
_check_banned_ast = check_banned_ast


# ===================================================================
# T) Core boundary — comprehensive AST-based check
# ===================================================================


class TestCoreBoundaryComprehensive:
    """Core modules must not import from adapters, runtime.builder, CLI, or transport SDKs.

    After the capacity/sanitization moves, core should be fully self-contained
    with only stdlib, medre.core.*, and a few generic dependencies.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.adapters",
        "medre.runtime.builder",
        "medre.runtime.route_engine",
        "medre.runtime.app",
        "medre.cli",
        "medre.config",
        "medre.runtime",
        "medre.observability",
        # Transport SDKs
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "meshcore",
        "RNS",
        "lxmf",
    )

    def test_core_files_have_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        core_dir = repo_root / "src" / "medre" / "core"
        assert core_dir.exists(), f"core directory not found: {core_dir}"

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            if "__pycache__" in str(py_file):
                continue
            rel = str(py_file.relative_to(repo_root))
            source = py_file.read_text()
            try:
                imports = _runtime_imports(source)
            except SyntaxError:
                violations.append(f"{rel}: syntax error, cannot parse")
                continue
            violations.extend(
                _check_banned_ast(imports, self._BANNED_PREFIXES, rel_path=rel)
            )

        assert violations == [], "Core files contain banned imports:\n" + "\n".join(
            violations
        )


# ===================================================================
# U) Route engine boundary — comprehensive check
# ===================================================================


class TestRouteEngineBoundaryComprehensive:
    """Route engine must not import adapter implementations or SDKs.

    It may use platform strings like 'matrix' and 'meshtastic' for
    channel_room_map expansion, but must not import adapter modules.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.adapters",
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
        "medre.runtime.builder",
        "medre.runtime.app",
        "medre.cli",
    )

    def test_route_engine_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        route_engine = repo_root / "src" / "medre" / "runtime" / "route_engine.py"
        assert route_engine.exists(), f"route_engine.py not found: {route_engine}"

        rel = str(route_engine.relative_to(repo_root))
        source = route_engine.read_text()
        imports = _all_imports(source)
        violations = _check_banned_ast(imports, self._BANNED_PREFIXES, rel_path=rel)

        assert (
            violations == []
        ), "route_engine.py contains banned imports:\n" + "\n".join(violations)


# ===================================================================
# V) Config model boundary — comprehensive check
# ===================================================================


class TestConfigModelBoundaryComprehensive:
    """config/model.py may import adapter config dataclasses but not
    adapter implementations.

    Allowed: medre.config.adapters.* (dataclasses only)
    Disallowed: medre.adapters.*.adapter, medre.adapters.*.session,
                medre.runtime.builder, medre.runtime.route_engine,
                medre.core.engine, nio, meshtastic, aiohttp, serial
    """

    _BANNED_TOP_LEVEL: tuple[str, ...] = (
        "medre.adapters.matrix.adapter",
        "medre.adapters.matrix.session",
        "medre.adapters.meshtastic.adapter",
        "medre.adapters.meshtastic.session",
        "medre.adapters.meshcore.adapter",
        "medre.adapters.meshcore.session",
        "medre.adapters.lxmf.adapter",
        "medre.adapters.lxmf.session",
        "medre.runtime.builder",
        "medre.runtime.route_engine",
        "medre.core.engine",
        # SDKs
        "nio",
        "meshtastic",
        "aiohttp",
        "serial",
    )

    # medre.runtime.routes is allowed ONLY under TYPE_CHECKING or deferred
    _RUNTIME_ROUTES_MODULE = "medre.runtime.routes"

    def test_config_model_no_banned_imports(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        model_file = repo_root / "src" / "medre" / "config" / "model.py"
        assert model_file.exists(), f"config/model.py not found: {model_file}"

        rel = str(model_file.relative_to(repo_root))
        source = model_file.read_text()

        # Check runtime-scope imports for banned items
        rt_imports = _runtime_imports(source)
        violations = _check_banned_ast(rt_imports, self._BANNED_TOP_LEVEL, rel_path=rel)

        # Also check that medre.runtime.routes is NOT a bare runtime-scope import
        # (it must be under TYPE_CHECKING or deferred inside a function body)
        for mod, lineno in rt_imports:
            if mod == self._RUNTIME_ROUTES_MODULE or mod.startswith(
                self._RUNTIME_ROUTES_MODULE + "."
            ):
                # Verify it is inside an `if TYPE_CHECKING:` block
                # by checking the source line context
                lines = source.splitlines()
                # Look backward from lineno for `if TYPE_CHECKING`
                in_type_checking = False
                for check_line in range(lineno - 1, max(0, lineno - 10), -1):
                    if "TYPE_CHECKING" in lines[check_line]:
                        in_type_checking = True
                        break
                    if lines[check_line].strip() and not lines[
                        check_line
                    ].strip().startswith(("from ", "import ")):
                        break
                if not in_type_checking:
                    violations.append(
                        f"{rel}:{lineno}: top-level import of {mod} "
                        "(must be under TYPE_CHECKING or deferred)"
                    )

        assert (
            violations == []
        ), "config/model.py contains banned imports:\n" + "\n".join(violations)


# ===================================================================
# W) Reusable adapter module boundary
# ===================================================================


class TestReusableAdapterModuleBoundary:
    """Reusable adapter modules (codec/renderer/session) must not import
    runtime/builder/pipeline/storage/CLI or other transport adapter modules.
    """

    _BANNED_PREFIXES: tuple[str, ...] = (
        "medre.runtime",
        "medre.cli",
        "medre.core.engine",
        "medre.core.storage",
    )

    # Heavy SDK packages banned at top-level for codec/renderer files.
    _HEAVY_SDKS: tuple[str, ...] = ("nio", "meshtastic", "meshcore", "RNS", "lxmf")

    # Modules to scan.  Tuple of (path_suffix, transport_name).
    _MODULE_SPECS: list[tuple[str, str]] = [
        ("src/medre/adapters/matrix/codec.py", "matrix"),
        ("src/medre/adapters/matrix/renderer.py", "matrix"),
        ("src/medre/adapters/matrix/session.py", "matrix"),
        ("src/medre/adapters/meshtastic/codec.py", "meshtastic"),
        ("src/medre/adapters/meshtastic/renderer.py", "meshtastic"),
        ("src/medre/adapters/meshtastic/session.py", "meshtastic"),
        ("src/medre/adapters/meshcore/codec.py", "meshcore"),
        ("src/medre/adapters/meshcore/renderer.py", "meshcore"),
        ("src/medre/adapters/meshcore/session.py", "meshcore"),
        ("src/medre/adapters/lxmf/codec.py", "lxmf"),
        ("src/medre/adapters/lxmf/renderer.py", "lxmf"),
        ("src/medre/adapters/lxmf/session.py", "lxmf"),
        ("src/medre/interop/mmrelay.py", ""),
    ]

    def _check_module(self, py_file: Path, rel: str, transport: str) -> list[str]:
        """Check a single module for boundary violations."""
        source = py_file.read_text()
        violations: list[str] = []

        try:
            _ast.parse(source)
        except SyntaxError:
            return [f"{rel}: syntax error, cannot parse"]

        is_codec_or_renderer = py_file.name in ("codec.py", "renderer.py")

        # Gather top-level vs nested imports
        all_imports_list = _all_imports(source, file_path=str(py_file))
        top_imports = _top_level_imports(source, file_path=str(py_file))

        # 1. Check all imports for banned prefixes (runtime, cli, core.engine, core.storage)
        for mod, lineno in all_imports_list:
            for prefix in self._BANNED_PREFIXES:
                if mod == prefix or mod.startswith(prefix + "."):
                    violations.append(
                        f"{rel}:{lineno}: imports {mod} (banned: {prefix})"
                    )
                    break

        # 2. Check own-adapter.module import (e.g. matrix/codec.py importing matrix/adapter)
        if transport:
            own_adapter = f"medre.adapters.{transport}.adapter"
            for mod, lineno in all_imports_list:
                if mod == own_adapter or mod.startswith(own_adapter + "."):
                    violations.append(
                        f"{rel}:{lineno}: imports {mod} "
                        f"(circular: reusable module importing own adapter)"
                    )

        # 2b. Cross-adapter isolation: reusable modules must not import
        #     other transport adapter packages (e.g. matrix/codec importing
        #     meshtastic/*).  interop modules are exempt.
        if transport:
            for mod, lineno in all_imports_list:
                if not mod.startswith("medre.adapters."):
                    continue
                # e.g. "medre.adapters.meshtastic.codec"
                parts = mod.split(".")
                if len(parts) >= 3:
                    other_transport = parts[2]
                    if other_transport != transport and other_transport != "":
                        violations.append(
                            f"{rel}:{lineno}: imports {mod} "
                            f"(cross-adapter: {transport} module importing "
                            f"{other_transport})"
                        )

        # 3. Codec/renderer must NOT have top-level heavy SDK imports
        if is_codec_or_renderer:
            for mod, lineno in top_imports:
                for sdk in self._HEAVY_SDKS:
                    if mod == sdk or mod.startswith(sdk + "."):
                        violations.append(
                            f"{rel}:{lineno}: top-level SDK import {mod} "
                            "(codec/renderer must not import heavy SDKs)"
                        )
                        break

        return violations

    def test_reusable_modules_boundary(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        violations: list[str] = []

        for path_suffix, transport in self._MODULE_SPECS:
            py_file = repo_root / path_suffix
            if not py_file.exists():
                continue
            rel = str(py_file.relative_to(repo_root))
            violations.extend(self._check_module(py_file, rel, transport))

        assert (
            violations == []
        ), "Reusable adapter module boundary violations:\n" + "\n".join(violations)
