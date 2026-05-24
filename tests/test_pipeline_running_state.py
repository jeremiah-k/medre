"""Tests for PipelineRunner.running state property.

Covers:
- ``running`` is ``False`` before start.
- ``running`` is ``True`` after successful start.
- ``running`` is ``False`` after stop.
- ``running`` remains ``False`` when stop is called without a prior start.
- ``running`` is ``True`` after repeated start calls (idempotent).
- ``running`` resets to ``False`` if start() fails after partially starting.
"""

from __future__ import annotations

import pytest

from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.routing import Router
from medre.core.storage import SQLiteStorage
from tests.helpers.pipeline import make_pipeline_config_for_pipeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_storage(tmp_path):
    """Provide a temporary SQLiteStorage instance."""
    storage = SQLiteStorage(db_path=str(tmp_path / "test.db"))
    return storage


@pytest.fixture
def pipeline_config(temp_storage: SQLiteStorage) -> PipelineConfig:
    """Minimal PipelineConfig for lifecycle tests (no adapters needed)."""
    return make_pipeline_config_for_pipeline(
        storage=temp_storage,
        router=Router(),
        adapters={},
    )


# ===================================================================
# TestPipelineRunningState
# ===================================================================


class TestPipelineRunningState:
    """Tests for the PipelineRunner.running property lifecycle."""

    async def test_running_false_before_start(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """``running`` must be ``False`` on a freshly constructed runner."""
        runner = PipelineRunner(pipeline_config)
        assert runner.running is False

    async def test_running_true_after_start(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """``running`` must be ``True`` after ``start()`` succeeds."""
        runner = PipelineRunner(pipeline_config)
        await runner.start()
        try:
            assert runner.running is True
        finally:
            await runner.stop()

    async def test_running_false_after_stop(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """``running`` must be ``False`` after ``stop()`` completes."""
        runner = PipelineRunner(pipeline_config)
        await runner.start()
        await runner.stop()
        assert runner.running is False

    async def test_running_false_when_stop_without_start(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """Calling ``stop()`` without a prior ``start()`` must keep ``running`` as ``False``."""
        runner = PipelineRunner(pipeline_config)
        assert runner.running is False
        await runner.stop()
        assert runner.running is False

    async def test_running_after_repeated_start(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """Repeated ``start()`` calls must leave ``running`` as ``True``.

        The second call must be idempotent — middleware is not
        registered twice.
        """
        runner = PipelineRunner(pipeline_config)
        await runner.start()
        # Count middleware entries after first start.
        mw_count_after_first = len(pipeline_config.event_bus._middleware)
        await runner.start()
        try:
            assert runner.running is True
            # Second start must not duplicate middleware.
            assert len(pipeline_config.event_bus._middleware) == mw_count_after_first
        finally:
            await runner.stop()

    async def test_running_false_after_start_stop_cycle(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """Full start-stop-start-stop cycle must track state correctly."""
        runner = PipelineRunner(pipeline_config)
        assert runner.running is False

        await runner.start()
        assert runner.running is True

        await runner.stop()
        assert runner.running is False

        await runner.start()
        assert runner.running is True

        await runner.stop()
        assert runner.running is False

    async def test_running_resets_on_start_failure(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """If ``start()`` raises after partially starting, ``running`` resets to ``False``."""
        runner = PipelineRunner(pipeline_config)
        # Sabotage the event bus so start() fails during middleware
        # registration.  The add_middleware call will raise because
        # event_bus is a plain EventBus — we'll patch it to raise.
        original_add = pipeline_config.event_bus.add_middleware

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated startup failure")

        pipeline_config.event_bus.add_middleware = _boom  # type: ignore[attr-defined]

        with pytest.raises(RuntimeError, match="simulated startup failure"):
            await runner.start()

        assert runner.running is False

        # Restore so teardown doesn't break.
        pipeline_config.event_bus.add_middleware = original_add  # type: ignore[attr-defined]

    async def test_running_is_read_only(self, pipeline_config: PipelineConfig) -> None:
        """``running`` must be a read-only property — setting it must raise."""
        runner = PipelineRunner(pipeline_config)
        with pytest.raises(AttributeError):
            runner.running = True  # type: ignore[misc]


class TestStartPartialFailureRollback:
    """PipelineRunner.start() rolls back middleware on partial failure."""

    async def test_middleware_removed_on_renderer_failure(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """If renderer platform registration fails, middleware is removed."""
        runner = PipelineRunner(pipeline_config)
        mw_before = len(pipeline_config.event_bus._middleware)

        # Sabotage _populate_renderer_platforms to fail after middleware
        # has been registered.
        original_populate = runner._populate_renderer_platforms

        def _boom_populate() -> None:
            raise RuntimeError("renderer platform registration failed")

        runner._populate_renderer_platforms = _boom_populate  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="renderer platform registration failed"):
            await runner.start()

        assert runner.running is False
        # Middleware count must be unchanged — the rollback removed it.
        assert len(pipeline_config.event_bus._middleware) == mw_before

        # Runner must be reusable after the failure condition is removed.
        runner._populate_renderer_platforms = original_populate  # type: ignore[assignment]
        await runner.start()
        try:
            assert runner.running is True
        finally:
            await runner.stop()

    async def test_clean_restart_after_partial_failure(
        self, pipeline_config: PipelineConfig
    ) -> None:
        """Runner can start cleanly after a previous partial failure."""
        runner = PipelineRunner(pipeline_config)

        # First attempt: fail during renderer population.
        def _boom() -> None:
            raise RuntimeError("transient failure")

        runner._populate_renderer_platforms = _boom  # type: ignore[assignment]

        with pytest.raises(RuntimeError):
            await runner.start()

        assert runner.running is False

        # Second attempt: succeed after removing the failure condition.
        runner._populate_renderer_platforms = lambda: None  # type: ignore[assignment]
        await runner.start()
        try:
            assert runner.running is True
        finally:
            await runner.stop()
