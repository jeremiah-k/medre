"""Shutdown invariants for the rebuildable conversation projection."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from medre.config.model import (
    AdapterConfigSet,
    MatrixRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.runtime.builder import RuntimeBuilder


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


def _build_app(paths: MedrePaths):
    config = RuntimeConfig(
        runtime=RuntimeOptions(name="projection-shutdown-test"),
        storage=StorageConfig(backend="memory"),
        adapters=AdapterConfigSet(
            matrix={
                "main": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                    config=None,
                )
            }
        ),
    )
    return RuntimeBuilder(config, paths).build()


async def test_empty_final_drain_snapshot_does_not_persist_abandonment(
    tmp_paths: MedrePaths,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A deadline edge with no remaining work is a clean drain, not abandonment."""
    app = _build_app(tmp_paths)
    await app.start()
    persist_calls = 0

    async def _track_abandonment() -> None:
        nonlocal persist_calls
        persist_calls += 1

    app._persist_drain_abandoned_evidence = _track_abandonment  # type: ignore[method-assign]
    object.__setattr__(app.config.limits, "shutdown_drain_timeout_seconds", 0.0)

    with caplog.at_level(logging.WARNING, logger="medre.runtime.app"):
        await app.stop()

    assert persist_calls == 0
    assert not any("in-flight abandoned" in record.getMessage() for record in caplog.records)


async def test_projection_repair_failure_prevents_clean_shutdown_marker(
    tmp_paths: MedrePaths,
) -> None:
    """Any runtime repair failure keeps the startup projection marker dirty."""
    app = _build_app(tmp_paths)
    await app.start()
    clean_calls = 0

    async def _track_clean() -> None:
        nonlocal clean_calls
        clean_calls += 1

    app.pipeline_runner.mark_conversation_projection_clean = _track_clean  # type: ignore[method-assign]
    app.pipeline_runner._record_conversation_projection_repair_failure()

    await app.stop()

    assert app.pipeline_runner.conversation_projection_repair_failed is True
    assert clean_calls == 0
