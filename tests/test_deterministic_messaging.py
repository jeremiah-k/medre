"""Deterministic messaging and no-traceback assertion tests.

Validates that every error path an operator might encounter produces
clean, deterministic output without Python tracebacks or variable content
(timestamps, memory addresses, etc.). Also asserts that boot summaries
and supervision snapshots have stable, alphabetically-ordered key sets
and consistent classification results.

Split from the former ``tests/test_operator_recovery.py`` monolith.
Shared fixtures/helpers live in ``tests/helpers/operator_recovery.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.errors import ConfigFileError, ConfigNotFoundError
from medre.config.loader import load_config
from medre.config.paths import MedrePaths, resolve
from medre.core.lifecycle.states import AdapterState
from medre.core.supervision.supervision import (
    RuntimeHealth,
    StartupOutcome,
    classify_runtime_health,
    classify_startup_outcome,
    runtime_supervision_snapshot,
)
from medre.runtime.boot_summary import build_boot_summary
from tests.helpers.operator_recovery import (
    CONFIG_BAD_YAML,
    CONFIG_MISSING_ADAPTER_REF,
    _build_app,
    _config_with_fake_adapters,
    _FailingAdapter,
    _run_cli_raw,
    _write_config,
)

# ---------------------------------------------------------------------------
# Fixtures (re-declared locally; pytest does not discover imported fixtures
# from non-conftest helper modules — see tests/helpers/startup_cleanup.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at temp directories."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# No-traceback assertions for config error paths
# ---------------------------------------------------------------------------


def test_config_not_found_no_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ConfigNotFoundError message contains no traceback."""
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    with pytest.raises(ConfigNotFoundError) as exc_info:
        load_config(None)
    msg = str(exc_info.value)
    assert "Traceback" not in msg
    assert "File " not in msg


def test_bad_yaml_no_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ConfigFileError for bad YAML contains no traceback."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_BAD_YAML)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    with pytest.raises(ConfigFileError) as exc_info:
        load_config(None)
    msg = str(exc_info.value)
    assert "Traceback" not in msg
    # YAML parse errors always reference the source file path.
    assert "config.yaml" in msg


def test_cli_config_check_no_traceback_on_any_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 'config check' never shows tracebacks for any config error."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_BAD_YAML)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    stdout, stderr, code = _run_cli_raw("config", "check")
    assert "Traceback" not in stdout
    assert "Traceback" not in stderr
    assert code != 0


def test_cli_routes_validate_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 'routes validate' never shows tracebacks."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_MISSING_ADAPTER_REF)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    stdout, stderr, code = _run_cli_raw("routes", "validate")
    assert "Traceback" not in stdout
    assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# Deterministic boot summary shape
# ---------------------------------------------------------------------------


def test_boot_summary_deterministic_ordering(tmp_paths: MedrePaths) -> None:
    """Boot summary to_dict() has deterministic key ordering."""
    bs = build_boot_summary(
        startup_timestamp="2026-05-11T12:00:00+00:00",
        startup_outcome="success",
        runtime_health="healthy",
        adapters_total=2,
        adapters_started=2,
        adapters_failed=0,
        adapters_disabled=0,
        build_failure_count=0,
        started_adapter_ids=["b_adapter", "a_adapter"],
        failed_adapter_ids=[],
        route_count=1,
        storage_backend="memory",
        replay_available=False,
        persisted_events_count=0,
    )
    d = bs.to_dict()
    keys = list(d.keys())
    assert keys == sorted(keys)


def test_boot_summary_partial_startup_deterministic(
    tmp_paths: MedrePaths,
) -> None:
    """Boot summary for partial startup has consistent shape."""
    bs = build_boot_summary(
        startup_timestamp="2026-05-11T12:00:00+00:00",
        startup_outcome="partial",
        runtime_health="degraded",
        adapters_total=3,
        adapters_started=1,
        adapters_failed=2,
        adapters_disabled=0,
        build_failure_count=0,
        started_adapter_ids=["working"],
        failed_adapter_ids=["broken_1", "broken_2"],
        route_count=2,
        storage_backend="sqlite",
        replay_available=True,
        persisted_events_count=5,
    )
    d = bs.to_dict()
    assert d["startup_outcome"] == "partial"
    assert d["runtime_health"] == "degraded"
    assert d["adapters_total"] == 3
    assert d["adapters_started"] == 1
    assert d["adapters_failed"] == 2
    # Keys are alphabetically sorted.
    assert list(d.keys()) == sorted(d.keys())


def test_supervision_snapshot_deterministic_keys() -> None:
    """runtime_supervision_snapshot has stable key set."""
    states = [AdapterState.READY, AdapterState.FAILED]
    snap = runtime_supervision_snapshot(states)
    assert "runtime_health" in snap
    assert "adapter_summary" in snap
    # adapter_summary has stable sub-keys.
    summary = snap["adapter_summary"]
    assert "total" in summary
    assert "healthy" in summary
    assert "failed" in summary


def test_classify_runtime_health_deterministic() -> None:
    """classify_runtime_health returns consistent results."""
    assert classify_runtime_health([AdapterState.READY]) == RuntimeHealth.HEALTHY
    assert (
        classify_runtime_health([AdapterState.READY, AdapterState.READY])
        == RuntimeHealth.HEALTHY
    )
    assert (
        classify_runtime_health([AdapterState.READY, AdapterState.FAILED])
        == RuntimeHealth.DEGRADED
    )
    assert classify_runtime_health([AdapterState.FAILED]) == RuntimeHealth.FAILED
    assert classify_runtime_health([]) == RuntimeHealth.FAILED


def test_classify_startup_outcome_deterministic() -> None:
    """classify_startup_outcome returns consistent results."""
    assert (
        classify_startup_outcome(started=2, failed=0, total=2) == StartupOutcome.SUCCESS
    )
    assert (
        classify_startup_outcome(started=1, failed=2, total=3) == StartupOutcome.PARTIAL
    )
    assert (
        classify_startup_outcome(started=0, failed=1, total=1)
        == StartupOutcome.TOTAL_FAILURE
    )


@pytest.mark.asyncio
async def test_degraded_boot_summary_no_variable_content(
    tmp_paths: MedrePaths,
) -> None:
    """Boot summary for degraded runtime has no variable content in static fields."""
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)
    app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

    await app.start()
    try:
        boot = app.boot_summary
        assert boot is not None
        d = boot.to_dict()

        # Static fields should be deterministic.
        assert d["startup_outcome"] == "partial"
        assert d["runtime_health"] == "degraded"
        assert d["adapters_total"] == 2
        assert d["adapters_started"] == 1
        assert d["adapters_failed"] == 1

        # failed_adapter_ids should contain the expected ID.
        assert "fake_mesh" in d["failed_adapter_ids"]
    finally:
        await app.stop()
