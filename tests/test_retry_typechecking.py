"""Tests for TYPE_CHECKING imports and RetryWorker/RetryWorkerState construction.

Verifies that ``medre.runtime.retry`` imports cleanly (lines 21-25 are
TYPE_CHECKING-guarded and must not execute at runtime), that the public
RetryWorkerState dataclass has correct defaults, and that RetryWorker can
be constructed with mock dependencies matching the type-hinted constructor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import medre.runtime.retry as retry_module
from medre.runtime.retry import RetryWorker, RetryWorkerState

# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------


class TestRetryModuleImport:
    """Verify the retry module loads without errors."""

    def test_import_retry_module(self) -> None:
        """Module imports cleanly and exposes expected __all__."""
        assert "RetryWorker" in retry_module.__all__
        assert "RetryWorkerState" in retry_module.__all__

    def test_type_checking_block_not_executed(self) -> None:
        """TYPE_CHECKING imports (lines 21-25) are NOT available at runtime."""
        # These names exist only under `if TYPE_CHECKING` and should not
        # be importable from the module namespace at runtime.
        assert not hasattr(retry_module, "PipelineRunner")
        assert not hasattr(retry_module, "CapacityController")
        assert not hasattr(retry_module, "SQLiteStorage")
        assert not hasattr(retry_module, "EventBuffer")


# ---------------------------------------------------------------------------
# RetryWorkerState
# ---------------------------------------------------------------------------


class TestRetryWorkerState:
    """Tests for the RetryWorkerState dataclass."""

    def test_retry_worker_state_defaults(self) -> None:
        """RetryWorkerState has correct default values."""
        state = RetryWorkerState()
        assert state.enabled is False
        assert state.running is False
        assert state.last_run_at is None
        assert state.processed == 0
        assert state.succeeded == 0
        assert state.failed == 0
        assert state.dead_lettered == 0

    def test_retry_worker_state_enabled_constructor(self) -> None:
        """RetryWorkerState can be constructed with enabled=True."""
        state = RetryWorkerState(enabled=True)
        assert state.enabled is True
        assert state.running is False

    def test_retry_worker_state_mutates(self) -> None:
        """State counters can be incremented and fields updated."""
        state = RetryWorkerState()

        state.processed += 1
        state.succeeded += 1
        assert state.processed == 1
        assert state.succeeded == 1

        state.processed += 1
        state.failed += 1
        assert state.processed == 2
        assert state.failed == 1

        state.dead_lettered += 1
        assert state.dead_lettered == 1

        state.running = True
        assert state.running is True

        state.last_run_at = "2026-05-20T12:00:00+00:00"
        assert state.last_run_at == "2026-05-20T12:00:00+00:00"


# ---------------------------------------------------------------------------
# RetryWorker construction
# ---------------------------------------------------------------------------


class TestRetryWorkerConstruction:
    """Tests for constructing RetryWorker with mock dependencies.

    This exercises the constructor parameter types that are declared via
    TYPE_CHECKING imports on lines 21-25 of retry.py.
    """

    def test_retry_worker_constructs_with_mocks(self) -> None:
        """RetryWorker can be constructed with mock storage/pipeline."""
        mock_storage = MagicMock()
        mock_pipeline = AsyncMock()
        mock_capacity = MagicMock()
        mock_event_buffer = MagicMock()

        worker = RetryWorker(
            storage=mock_storage,
            pipeline=mock_pipeline,
            capacity_controller=mock_capacity,
            enabled=False,
            event_buffer=mock_event_buffer,
        )

        assert worker.state is not None
        assert isinstance(worker.state, RetryWorkerState)
        assert worker.state.enabled is False

    def test_retry_worker_constructs_without_optional_deps(self) -> None:
        """RetryWorker works with None for optional capacity_controller and event_buffer."""
        mock_storage = MagicMock()
        mock_pipeline = AsyncMock()

        worker = RetryWorker(
            storage=mock_storage,
            pipeline=mock_pipeline,
            capacity_controller=None,
            enabled=False,
            event_buffer=None,
        )

        assert worker.state.enabled is False

    def test_retry_worker_state_reflects_enabled(self) -> None:
        """When enabled=True is passed, state.enabled is True."""
        mock_storage = MagicMock()
        mock_pipeline = AsyncMock()

        worker = RetryWorker(
            storage=mock_storage,
            pipeline=mock_pipeline,
            capacity_controller=None,
            enabled=True,
        )

        assert worker.state.enabled is True
        assert worker.state.running is False

    def test_retry_worker_custom_params(self) -> None:
        """RetryWorker accepts and surfaces custom interval, batch_size, max_attempts
        through public state and behavior (no exception)."""
        mock_storage = MagicMock()
        mock_pipeline = AsyncMock()

        worker = RetryWorker(
            storage=mock_storage,
            pipeline=mock_pipeline,
            capacity_controller=None,
            enabled=False,
            interval_seconds=5.0,
            batch_size=10,
            max_attempts=5,
        )

        # Construction should not raise; state must be valid.
        assert worker.state.enabled is False
        assert worker.state.processed == 0
