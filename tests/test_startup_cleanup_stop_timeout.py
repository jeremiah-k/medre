"""Startup-failure cleanup: adapter stop timeout and cancel supervision.

Split from ``test_startup_cleanup_stop_supervision.py`` (which was itself
split from ``test_startup_build_failure_and_cleanup.py``).  These tests
cover the scenario where an adapter's ``stop()`` hangs or is cancelled
during startup-failure cleanup — verifying that pipeline runner stop and
storage close still proceed regardless.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import pytest

from medre.config.paths import MedrePaths, resolve
from medre.runtime.app import RuntimeState
from medre.runtime.errors import RuntimeStartupError
from tests.helpers.startup_cleanup import (
    CancelledStopOnStartFailure,
    FailingAdapter,
    SlowStopOnStartFailure,
    _build_app,
    _config_with_one_fake_adapter,
    _config_with_two_fake_adapters,
    _make_tracking_pipeline_stop,
    _make_tracking_storage_close,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


@contextmanager
def _set_shutdown_timeout(app: object, seconds: float) -> Generator[None, None, None]:
    """Temporarily set ``config.runtime.shutdown_timeout_seconds``."""
    object.__setattr__(
        app.config.runtime,  # type: ignore[attr-defined]
        "shutdown_timeout_seconds",
        seconds,
    )
    try:
        yield
    finally:
        object.__setattr__(
            app.config.runtime,  # type: ignore[attr-defined]
            "shutdown_timeout_seconds",
            10,
        )


# ===================================================================
# Startup cleanup stop-timeout supervision
# ===================================================================


class TestStartupCleanupStopTimeout:
    """Startup failure cleanup uses wait_for so slow/hung adapter stops
    don't block pipeline runner stop or storage close."""

    @pytest.mark.asyncio
    async def test_slow_adapter_stop_during_startup_cleanup_does_not_block_storage(
        self, tmp_paths: MedrePaths
    ) -> None:
        """A slow-stopping adapter during startup failure cleanup doesn't
        prevent storage close."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        # Replace adapter with one that fails start and sleeps forever on stop.
        app.adapters["fake_matrix"] = SlowStopOnStartFailure(adapter_id="fake_matrix")

        storage_called = _make_tracking_storage_close(app)

        # Short timeout so the slow stop gets cut short quickly.
        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()

        # Adapter stop should have been called.
        assert app.adapters["fake_matrix"].stop_called

        # Storage MUST have been closed despite the slow adapter.
        assert storage_called[0], "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_slow_adapter_stop_during_startup_cleanup_does_not_block_pipeline(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Pipeline runner is stopped even when adapter stop times out during
        startup failure cleanup."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = SlowStopOnStartFailure(adapter_id="fake_matrix")

        pipeline_called = _make_tracking_pipeline_stop(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()

        assert pipeline_called[0], "pipeline_runner.stop() did not complete"

    @pytest.mark.asyncio
    async def test_cancelled_error_during_startup_cleanup_stop(
        self, tmp_paths: MedrePaths
    ) -> None:
        """CancelledError from adapter stop during startup best-effort
        cleanup (after start failure) is caught and suppressed (pattern C);
        pipeline and storage cleanup proceed.  The CancelledError is
        raised in the per-adapter best-effort stop in ``start()``, NOT
        in ``_cleanup_started_adapters()``.  The cancellation state is
        drained so subsequent adapter stops in the loop can still run."""
        config = _config_with_one_fake_adapter()
        app = _build_app(config, tmp_paths)

        app.adapters["fake_matrix"] = CancelledStopOnStartFailure(
            adapter_id="fake_matrix"
        )

        pipeline_called = _make_tracking_pipeline_stop(app)
        storage_called = _make_tracking_storage_close(app)

        with pytest.raises(RuntimeStartupError, match="Total startup failure"):
            await app.start()

        assert app.adapters["fake_matrix"].stop_called
        assert pipeline_called[0], "pipeline_runner.stop() did not complete"
        assert storage_called[0], "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED

    @pytest.mark.asyncio
    async def test_multiple_slow_adapters_all_attempted(
        self, tmp_paths: MedrePaths
    ) -> None:
        """Multiple slow adapters during startup cleanup: all are attempted,
        pipeline/storage cleanup still happens."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Both adapters fail to start and have slow stops.
        alpha = SlowStopOnStartFailure(adapter_id="alpha")
        beta = SlowStopOnStartFailure(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        storage_called = _make_tracking_storage_close(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()

        # Both adapters should have had stop called.
        assert alpha.stop_called, "alpha stop() not called"
        assert beta.stop_called, "beta stop() not called"

        # Storage close must still happen.
        assert storage_called[0], "storage.close() did not complete"

    @pytest.mark.asyncio
    async def test_mixed_slow_and_fast_adapters_during_startup_cleanup(
        self, tmp_paths: MedrePaths
    ) -> None:
        """One slow + one fast adapter during startup cleanup: both attempted,
        pipeline/storage still cleaned up."""
        config = _config_with_two_fake_adapters()
        app = _build_app(config, tmp_paths)

        # Alpha: slow stop, start fails.
        alpha = SlowStopOnStartFailure(adapter_id="alpha")
        # Beta: fast stop (FailingAdapter), start fails.
        beta = FailingAdapter(adapter_id="beta")
        app.adapters["alpha"] = alpha
        app.adapters["beta"] = beta

        storage_called = _make_tracking_storage_close(app)

        with _set_shutdown_timeout(app, 0.2):
            with pytest.raises(RuntimeStartupError, match="Total startup failure"):
                await app.start()

        assert alpha.stop_called
        assert storage_called[0], "storage.close() did not complete"
        assert app.state == RuntimeState.FAILED
