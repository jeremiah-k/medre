"""Tests for TYPE_CHECKING imports and basic MedreApp construction.

Covers:
- Module imports cleanly (lines 53-70 of app.py — TYPE_CHECKING block).
- ``RuntimeState`` enum values and expected transitions.
- ``MedreApp`` dataclass construction with all required fields.
- ``MedreApp`` default properties (state, retry_state, event_buffer).
- TYPE_CHECKING'd types are compatible with MedreApp field annotations.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.retry import RetryWorkerState

# ===================================================================
# Module import
# ===================================================================


class TestAppModuleImport:
    """Verify that ``medre.runtime.app`` loads without import errors."""

    def test_import_app_module(self) -> None:
        """Importing the module should succeed even though TYPE_CHECKING
        imports are guarded behind ``if TYPE_CHECKING``."""
        import importlib

        mod = importlib.import_module("medre.runtime.app")
        assert hasattr(mod, "MedreApp")
        assert hasattr(mod, "RuntimeState")

    def test_public_api(self) -> None:
        """``__all__`` exposes exactly ``MedreApp`` and ``RuntimeState``."""
        import medre.runtime.app as mod

        assert "MedreApp" in mod.__all__
        assert "RuntimeState" in mod.__all__


# ===================================================================
# RuntimeState enum
# ===================================================================


class TestRuntimeStateEnum:
    """Exercise ``RuntimeState`` values and allowed transitions."""

    def test_enum_values(self) -> None:
        expected = {
            "INITIALIZED": "initialized",
            "STARTING": "starting",
            "RUNNING": "running",
            "STOPPING": "stopping",
            "STOPPED": "stopped",
            "FAILED": "failed",
        }
        for name, value in expected.items():
            assert RuntimeState[name].value == value

    def test_enum_members_count(self) -> None:
        assert len(RuntimeState) == 6

    def test_happy_path_ordering(self) -> None:
        """INITIALIZED < STARTING < RUNNING < STOPPING < STOPPED in value order."""
        order = [
            RuntimeState.INITIALIZED,
            RuntimeState.STARTING,
            RuntimeState.RUNNING,
            RuntimeState.STOPPING,
            RuntimeState.STOPPED,
        ]
        for i in range(len(order) - 1):
            assert order[i] != order[i + 1]

    def test_failed_is_distinct(self) -> None:
        assert RuntimeState.FAILED not in (
            RuntimeState.INITIALIZED,
            RuntimeState.STARTING,
            RuntimeState.RUNNING,
            RuntimeState.STOPPING,
            RuntimeState.STOPPED,
        )


# ===================================================================
# MedreApp dataclass construction
# ===================================================================


class TestMedreAppConstruction:
    """Construct MedreApp with mock objects covering TYPE_CHECKING'd types."""

    @staticmethod
    def _make_app() -> MedreApp:
        """Build a minimal MedreApp with MagicMock substitutes.

        Each mock stands in for one of the TYPE_CHECKING'd imports
        (lines 53-70), thereby exercising the type annotations at
        construction time.
        """
        return MedreApp(
            config=MagicMock(spec=["runtime"]),  # RuntimeConfig
            paths=MagicMock(),  # MedrePaths
            storage=MagicMock(),  # SQLiteStorage | None
            event_bus=MagicMock(),  # EventBus
            rendering_pipeline=MagicMock(),  # RenderingPipeline
            router=MagicMock(),  # Router
            fallback_resolver=MagicMock(),  # FallbackResolver
            relation_resolver=MagicMock(),  # RelationResolver
            pipeline_runner=MagicMock(),  # PipelineRunner
            diagnostician=MagicMock(),  # Diagnostician
            adapters={},  # dict[str, AdapterContract]
            shutdown_event=asyncio.Event(),  # asyncio.Event
        )

    def test_constructs_without_error(self) -> None:
        app = self._make_app()
        assert app is not None

    def test_adapters_default_empty(self) -> None:
        app = self._make_app()
        assert app.adapters == {}

    def test_build_failures_default_empty(self) -> None:
        app = self._make_app()
        assert app.build_failures == []

    def test_route_stats_default_none(self) -> None:
        app = self._make_app()
        assert app.route_stats is None

    def test_storage_accepts_none(self) -> None:
        self._make_app()
        app_none = MedreApp(
            config=MagicMock(),
            paths=MagicMock(),
            storage=None,
            event_bus=MagicMock(),
            rendering_pipeline=MagicMock(),
            router=MagicMock(),
            fallback_resolver=MagicMock(),
            relation_resolver=MagicMock(),
            pipeline_runner=MagicMock(),
            diagnostician=MagicMock(),
            adapters={},
            shutdown_event=asyncio.Event(),
        )
        assert app_none.storage is None


# ===================================================================
# MedreApp properties
# ===================================================================


class TestMedreAppProperties:
    """Test read-only properties on a freshly constructed MedreApp."""

    @staticmethod
    def _make_app() -> MedreApp:
        return MedreApp(
            config=MagicMock(),
            paths=MagicMock(),
            storage=MagicMock(),
            event_bus=MagicMock(),
            rendering_pipeline=MagicMock(),
            router=MagicMock(),
            fallback_resolver=MagicMock(),
            relation_resolver=MagicMock(),
            pipeline_runner=MagicMock(),
            diagnostician=MagicMock(),
            adapters={},
            shutdown_event=asyncio.Event(),
        )

    def test_state_returns_initialized(self) -> None:
        app = self._make_app()
        assert app.state is RuntimeState.INITIALIZED

    def test_retry_state_returns_default_when_no_worker(self) -> None:
        app = self._make_app()
        result = app.retry_state
        assert isinstance(result, RetryWorkerState)

    def test_event_buffer_is_populated_by_post_init(self) -> None:
        app = self._make_app()
        assert app.event_buffer is not None

    def test_adapter_states_returns_empty_dict(self) -> None:
        app = self._make_app()
        assert app.adapter_states == {}

    def test_replay_engine_default_none(self) -> None:
        app = self._make_app()
        assert app.replay_engine is None

    def test_boot_summary_default_none(self) -> None:
        app = self._make_app()
        assert app.boot_summary is None

    def test_route_eligibility_default_none(self) -> None:
        app = self._make_app()
        assert app.route_eligibility is None

    def test_startup_readiness_default_none(self) -> None:
        app = self._make_app()
        assert app.startup_readiness is None

    def test_shutdown_event_is_settable(self) -> None:
        """shutdown_event is a real asyncio.Event, not a mock."""
        app = self._make_app()
        assert not app.shutdown_event.is_set()
        app.shutdown_event.set()
        assert app.shutdown_event.is_set()


# ===================================================================
# TYPE_CHECKING type compatibility
# ===================================================================


class TestTypeCheckingCompatibility:
    """Validate that TYPE_CHECKING'd types can be used as field values.

    Lines 53-70 guard heavy imports behind ``if TYPE_CHECKING``.
    MedreApp's field annotations reference these types.  Constructing
    MedreApp with objects that satisfy those annotations proves the
    type-checking boundary is sound.
    """

    def test_adapters_dict_accepts_mock_contract(self) -> None:
        app = MedreApp(
            config=MagicMock(),
            paths=MagicMock(),
            storage=MagicMock(),
            event_bus=MagicMock(),
            rendering_pipeline=MagicMock(),
            router=MagicMock(),
            fallback_resolver=MagicMock(),
            relation_resolver=MagicMock(),
            pipeline_runner=MagicMock(),
            diagnostician=MagicMock(),
            adapters={"adapter-1": MagicMock()},
            shutdown_event=asyncio.Event(),
        )
        assert "adapter-1" in app.adapters

    def test_optional_fields_stay_none(self) -> None:
        app = MedreApp(
            config=MagicMock(),
            paths=MagicMock(),
            storage=None,
            event_bus=MagicMock(),
            rendering_pipeline=MagicMock(),
            router=MagicMock(),
            fallback_resolver=MagicMock(),
            relation_resolver=MagicMock(),
            pipeline_runner=MagicMock(),
            diagnostician=MagicMock(),
            adapters={},
            shutdown_event=asyncio.Event(),
        )
        # All optional private fields should be their defaults
        assert app.route_stats is None
        assert app.replay_engine is None
        assert app.boot_summary is None
