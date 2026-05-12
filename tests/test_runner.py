"""Unit tests for medre.runner: config-driven bootstrap.

Tests the public API (``run`` and ``main``) with all dependencies mocked
so no real config files, adapters, or runtimes are needed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.runner import main, run


# ===================================================================
# TestRunnerFunctions
# ===================================================================


class TestRunnerFunctions:
    """Verify the runner module's public entry points exist and have the
    correct calling conventions."""

    def test_run_is_async(self) -> None:
        assert asyncio.iscoroutinefunction(run)

    def test_main_is_sync(self) -> None:
        assert callable(main)
        assert not asyncio.iscoroutinefunction(main)


# ===================================================================
# TestRunWithConfig
# ===================================================================


class TestRunWithConfig:
    """Test the async ``run()`` entry point with mocked dependencies."""

    async def test_run_loads_config_with_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``load_config`` is called with the provided config path."""
        calls: list[str | None] = []

        def fake_load_config(path: str | None = None) -> object:
            calls.append(path)
            raise RuntimeError("stop here")

        monkeypatch.setattr("medre.runner.load_config", fake_load_config)

        with pytest.raises(RuntimeError, match="stop here"):
            await run("/test/path.toml")

        assert calls == ["/test/path.toml"]

    async def test_run_loads_config_without_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``load_config`` is called with ``None`` when no path given."""
        calls: list[str | None] = []

        def fake_load_config(path: str | None = None) -> object:
            calls.append(path)
            raise RuntimeError("stop here")

        monkeypatch.setattr("medre.runner.load_config", fake_load_config)

        with pytest.raises(RuntimeError, match="stop here"):
            await run()

        assert calls == [None]

    async def test_run_no_config_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``run()`` propagates ``ConfigNotFoundError`` from ``load_config``."""
        from medre.config.errors import ConfigNotFoundError

        def fake_load_config(path: str | None = None) -> object:
            raise ConfigNotFoundError("no config found")

        monkeypatch.setattr("medre.runner.load_config", fake_load_config)

        with pytest.raises(ConfigNotFoundError):
            await run()

    async def test_run_full_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the full config → env → build → start → stop pipeline."""
        from types import SimpleNamespace

        config = SimpleNamespace(logging=SimpleNamespace(level="INFO"))
        paths = SimpleNamespace()

        # Mock every stage of the pipeline
        monkeypatch.setattr("medre.runner.load_config", lambda path=None: (config, "file", paths))
        monkeypatch.setattr("medre.runner.apply_env_overrides", lambda cfg, p: cfg)

        mock_app = MagicMock()
        mock_app.start = AsyncMock()
        mock_app.wait_for_shutdown = AsyncMock()
        mock_app.stop = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app
        monkeypatch.setattr("medre.runner.RuntimeBuilder", lambda cfg, p: mock_builder)

        # Silence signal handler setup
        monkeypatch.setattr("medre.runner.asyncio.get_running_loop", lambda: MagicMock())

        await run("/fake.toml")

        mock_builder.build.assert_called_once()
        mock_app.start.assert_awaited_once()
        mock_app.wait_for_shutdown.assert_awaited_once()
        mock_app.stop.assert_awaited_once()


# ===================================================================
# TestMainFunction
# ===================================================================


class TestMainFunction:
    """Test the synchronous ``main()`` entry point."""

    def test_main_passes_config_path(self) -> None:
        """``--config`` is forwarded to ``run()`` via ``asyncio.run``."""
        captured: list = []

        def fake_asyncio_run(coro: object) -> None:
            captured.append(coro)
            # Close the coroutine to prevent "was never awaited" warnings.
            # The test only verifies main() creates and passes the coroutine;
            # it does not need to execute it.
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]

        with patch("medre.runner.asyncio.run", fake_asyncio_run):
            main(["--config", "/test/path.toml"])

        assert len(captured) == 1
        assert asyncio.iscoroutine(captured[0])

    def test_main_no_args(self) -> None:
        """``main()`` works without ``--config`` (path is ``None``)."""
        captured: list = []

        def fake_asyncio_run(coro: object) -> None:
            captured.append(coro)
            # Close the coroutine to prevent "was never awaited" warnings.
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]

        with patch("medre.runner.asyncio.run", fake_asyncio_run):
            main([])

        assert len(captured) == 1

    def test_main_help(self) -> None:
        """``--help`` exits cleanly with code 0."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_main_no_unawaited_coroutine_warning(self) -> None:
        """Regression: ``main()`` mock must close the coroutine to avoid
        ``RuntimeWarning: coroutine 'run' was never awaited``.

        See: https://docs.python.org/3/library/asyncio-task.html#asyncio.close
        """
        import warnings

        captured: list = []

        def fake_asyncio_run(coro: object) -> None:
            captured.append(coro)
            if hasattr(coro, "close"):
                coro.close()  # type: ignore[union-attr]

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with patch("medre.runner.asyncio.run", fake_asyncio_run):
                main(["--config", "/test/path.toml"])

        runtime_warnings = [x for x in w if issubclass(x.category, RuntimeWarning)]
        unawaited = [
            x for x in runtime_warnings
            if "was never awaited" in str(x.message)
        ]
        assert len(unawaited) == 0, (
            f"Expected no unawaited coroutine warnings, got: {[str(x.message) for x in unawaited]}"
        )
