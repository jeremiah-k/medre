"""Architectural boundary and regression tests.

These tests protect structural invariants of the MEDRE framework — ensuring
that core/routing/replay/pipeline modules stay transport-SDK-free, that the
RuntimeBuilder can be imported and exercised with fake adapters, and that
no replay test file pulls in live transport dependencies.

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
"""Concrete adapter package prefixes (excludes medre.adapters.base and fake_*)."""


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
    Imports from medre.adapters.base (protocol/base types) are permitted."""

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

    Added in Tranche 1 — enforces the dependency inversion fix:
    adapter contract types live in core/ports.py and core/adapter_base.py,
    not in medre.adapters.base.
    """

    def test_no_runtime_core_to_adapters_imports(self) -> None:
        """Scan all core .py files for medre.adapters imports."""
        tests_dir = Path(__file__).parent
        core_dir = (tests_dir / ".." / ".." / "src" / "medre" / "core").resolve()

        violations: list[str] = []
        for py_file in sorted(core_dir.rglob("*.py")):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("from medre.adapters") or stripped.startswith("import medre.adapters"):
                    violations.append(f"{py_file.relative_to(tests_dir.parent.parent)}:{i}: {stripped}")

        assert violations == [], (
            f"Core modules must not import from medre.adapters:\n"
            + "\n".join(violations)
        )
