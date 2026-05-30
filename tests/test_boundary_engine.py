"""Engine and pipeline boundary tests.

Enforce that core engine modules (replay, route, pipeline) and the
RuntimeBuilder stay free of concrete transport SDK imports and that
replay test files use only fake adapters (sections A–F).

TRACK 6 — Boundary/Regression Tests
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from medre.runtime.architecture_report import _SDK_PACKAGES
from tests.helpers.import_scanner import (
    ADAPTER_PREFIXES,
    banned_imports,
    import_lines,
)
from tests.helpers.source_reader import source_of as _source_of

# ===================================================================
# A) ReplayEngine does not import transport SDKs
# ===================================================================


class TestReplayEngineBoundary:
    """ReplayEngine (src/medre/core/engine/replay/engine.py) must not import
    any concrete transport SDK or concrete adapter package."""

    def test_replay_engine_does_not_import_transport_sdks(self) -> None:
        source = _source_of("medre.core.engine.replay.engine")
        lines = import_lines(source)

        banned_sdk = banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"replay engine imports transport SDKs: {banned_sdk}"

        banned_adapters = banned_imports(lines, ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"replay engine imports concrete adapter packages: {banned_adapters}"


# ===================================================================
# B) RouteEngine does not import transport SDKs
# ===================================================================


class TestRouteEngineBoundary:
    """RouteEngine (src/medre/runtime/route_engine.py) must not import
    any concrete transport SDK or concrete adapter package."""

    def test_route_engine_does_not_import_transport_sdks(self) -> None:
        source = _source_of("medre.runtime.route_engine")
        lines = import_lines(source)

        banned_sdk = banned_imports(lines, _SDK_PACKAGES)
        assert banned_sdk == [], f"route_engine.py imports transport SDKs: {banned_sdk}"

        banned_adapters = banned_imports(lines, ADAPTER_PREFIXES)
        assert (
            banned_adapters == []
        ), f"route_engine.py imports concrete adapter packages: {banned_adapters}"


# ===================================================================
# C) PipelineRunner does not import concrete SDKs
# ===================================================================


class TestPipelineRunnerBoundary:
    """Pipeline package submodules (runner, target_delivery) must not import
    any concrete transport SDK or concrete adapter package.
    Adapter contract types live in medre.core.contracts.adapter — core
    must not import medre.adapters at runtime.

    After the pipeline package split, the package ``__init__.py`` is a thin
    re-export facade.  The substantive modules are ``runner`` and
    ``target_delivery``; these are the modules whose imports enforce the
    SDK/adapter boundary.
    """

    _PIPELINE_SUBMODULES = (
        "medre.core.engine.pipeline",
        "medre.core.engine.pipeline.runner",
        "medre.core.engine.pipeline.target_delivery",
    )

    def test_pipeline_submodules_do_not_import_concrete_sdks(self) -> None:
        for mod_name in self._PIPELINE_SUBMODULES:
            source = _source_of(mod_name)
            lines = import_lines(source)

            banned_sdk = banned_imports(lines, _SDK_PACKAGES)
            assert banned_sdk == [], f"{mod_name} imports transport SDKs: {banned_sdk}"

            banned_adapters = banned_imports(lines, ADAPTER_PREFIXES)
            assert (
                banned_adapters == []
            ), f"{mod_name} imports concrete adapter packages: {banned_adapters}"


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
        from medre.core.storage.sqlite.storage import SQLiteStorage

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
        from medre.adapters.fakes.matrix import FakeMatrixAdapter
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
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
            "test_replay_engine_modes.py",
            "test_replay_engine_count_and_state.py",
            "test_replay_engine_plan_filters.py",
            "test_replay_engine_diagnostics.py",
            "test_replay_routing.py",
            "test_replay_routing_controls.py",
            "test_replay_routing_isolation.py",
            "test_replay_routing_durability.py",
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
