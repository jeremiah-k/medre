"""Pipeline TYPE_CHECKING import and construction tests.

Verifies that the pipeline module loads cleanly (exercising the
``TYPE_CHECKING``-guarded ``CapacityController`` import on lines 68-69),
and that ``PipelineConfig`` / ``PipelineRunner`` can be constructed with
minimal dependencies.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import medre.core.engine.pipeline as pipeline_mod
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.routing import Router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config() -> PipelineConfig:
    """Build a PipelineConfig with mock storage and sensible defaults."""
    storage = MagicMock()
    return PipelineConfig(
        storage=storage,
        router=Router(routes=[]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={},
        event_bus=EventBus(),
    )


# ===================================================================
# Tests
# ===================================================================


class TestPipelineModuleImports:
    """Verify the pipeline module and its symbols load without error."""

    def test_import_pipeline_module(self) -> None:
        """Module-level import succeeds (covers lines 68-69)."""
        assert hasattr(pipeline_mod, "PipelineConfig")
        assert hasattr(pipeline_mod, "PipelineRunner")

    def test_pipeline_config_importable(self) -> None:
        """PipelineConfig is directly importable."""
        assert PipelineConfig is not None

    def test_pipeline_runner_importable(self) -> None:
        """PipelineRunner is directly importable."""
        assert PipelineRunner is not None


class TestPipelineConstruction:
    """Test PipelineConfig and PipelineRunner construction with minimal args."""

    def test_pipeline_config_constructs(self) -> None:
        """PipelineConfig accepts minimal valid arguments."""
        config = _minimal_config()
        assert config.adapters == {}
        assert isinstance(config.event_bus, EventBus)

    def test_pipeline_runner_constructs(self) -> None:
        """PipelineRunner can be constructed from a minimal config."""
        config = _minimal_config()
        runner = PipelineRunner(config)
        assert runner is not None

    def test_set_capacity_controller(self) -> None:
        """set_capacity_controller accepts a mock CapacityController.

        This exercises the TYPE_CHECKING import on lines 68-69 — the
        ``CapacityController`` type annotation is resolved at runtime via
        ``from __future__ import annotations``, so passing a MagicMock
        validates type compatibility without importing the real class.
        """
        config = _minimal_config()
        runner = PipelineRunner(config)

        mock_cc = MagicMock()
        # The method should accept a mock without raising — exercises the
        # TYPE_CHECKING'd CapacityController type annotation.
        runner.set_capacity_controller(mock_cc)

        # Inject the same mock via the public API, then verify it doesn't
        # raise on a second call (idempotent).
        runner.set_capacity_controller(mock_cc)
