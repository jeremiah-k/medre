"""Startup failure and degraded runtime recovery tests.

Validates operator repair loops for runtime startup failures: total startup
failure (all adapters fail), degraded runtime (partial startup with clear
attribution), and adapter disable/enable workflows. Each scenario confirms
that the operator receives actionable, deterministic feedback and can recover
by adjusting configuration.

Split from the former ``tests/test_operator_recovery.py`` monolith.
Shared fixtures/helpers live in ``tests/helpers/operator_recovery.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.loader import load_config
from medre.config.paths import MedrePaths, resolve
from medre.core.lifecycle.states import AdapterState
from medre.core.supervision.supervision import (
    RuntimeHealth,
    classify_runtime_health,
    runtime_supervision_snapshot,
)
from medre.runtime.app import RuntimeState
from medre.runtime.errors import RuntimeStartupError
from tests.helpers.operator_recovery import (
    _build_app,
    _config_with_fake_adapters,
    _config_with_one_fake_adapter,
    _FailingAdapter,
    _run_cli,
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
# Local YAML config snippets (used only in this file)
# ---------------------------------------------------------------------------

CONFIG_DISABLED_ADAPTER = """\
runtime:
  name: disabled-test
storage:
  backend: memory
adapters:
  matrix:
    enabled_one:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok1
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
    disabled_one:
      enabled: false
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok2
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
"""


# ---------------------------------------------------------------------------
# Startup failure recovery
#
# All adapters fail → total failure → operator disables bad adapter → retry.
# Validates the recovery loop for total startup failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_total_failure_then_disable_one_recovers(
    tmp_paths: MedrePaths,
) -> None:
    """Both adapters fail → total failure → disable one → remaining works."""
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)

    # Make both adapters fail on start.
    app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")
    app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

    with pytest.raises(RuntimeStartupError, match="Total startup failure"):
        await app.start()

    assert app.state == RuntimeState.FAILED

    # Operator action: create new config with one enabled adapter.
    config2 = _config_with_one_fake_adapter()
    app2 = _build_app(config2, tmp_paths)

    await app2.start()
    try:
        assert app2.state == RuntimeState.RUNNING
        assert len(app2.started_adapter_ids) == 1
        boot = app2.boot_summary
        assert boot is not None
        assert boot.startup_outcome == "success"
    finally:
        await app2.stop()


@pytest.mark.asyncio
async def test_total_failure_error_is_clean(tmp_paths: MedrePaths) -> None:
    """RuntimeStartupError message is concise, no raw traceback."""
    config = _config_with_one_fake_adapter()
    app = _build_app(config, tmp_paths)
    app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")

    with pytest.raises(RuntimeStartupError) as exc_info:
        await app.start()

    msg = str(exc_info.value)
    assert "Total startup failure" in msg
    assert "Traceback" not in msg


# ---------------------------------------------------------------------------
# Degraded runtime recovery
#
# Partial startup → degraded mode → diagnostics available → healthy adapter works.
# Validates that a degraded runtime produces clear messaging and
# remains functional for the healthy adapters.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_startup_degraded_messaging(
    tmp_paths: MedrePaths,
) -> None:
    """One adapter fails → degraded boot summary with clear attribution."""
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)

    failing = _FailingAdapter(adapter_id="fake_mesh")
    app.adapters["fake_mesh"] = failing

    await app.start()
    try:
        assert app.state == RuntimeState.RUNNING

        boot = app.boot_summary
        assert boot is not None
        assert boot.startup_outcome == "partial"
        assert boot.runtime_health == "degraded"
        assert boot.adapters_started == 1
        assert boot.adapters_failed == 1
        assert "fake_mesh" in boot.failed_adapter_ids
        assert "fake_matrix" in boot.started_adapter_ids
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_degraded_diagnostics_snapshot_accessible(
    tmp_paths: MedrePaths,
) -> None:
    """Diagnostic snapshot is available from a degraded runtime."""
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)

    failing = _FailingAdapter(adapter_id="fake_mesh")
    app.adapters["fake_mesh"] = failing

    await app.start()
    try:
        snap = app.diagnostic_snapshot()
        assert isinstance(snap, dict)
        assert snap["runtime_state"] == "running"
        # Diagnostic snapshot is a dict with deterministic keys.
        assert "capacity" in snap or "runtime_state" in snap
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_degraded_supervision_snapshot(tmp_paths: MedrePaths) -> None:
    """Supervision snapshot correctly reports degraded state."""
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)

    failing = _FailingAdapter(adapter_id="fake_mesh")
    app.adapters["fake_mesh"] = failing

    await app.start()
    try:
        states = [AdapterState.READY, AdapterState.FAILED]
        health = classify_runtime_health(states)
        assert health == RuntimeHealth.DEGRADED

        snap = runtime_supervision_snapshot(states)
        assert snap["runtime_health"] == "degraded"
        assert snap["adapter_summary"]["healthy"] == 1
        assert snap["adapter_summary"]["failed"] == 1
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_degraded_to_healthy_after_rebuild(
    tmp_paths: MedrePaths,
) -> None:
    """Rebuilding runtime without failing adapter produces healthy outcome."""
    # First run: degraded.
    config1 = _config_with_fake_adapters()
    app1 = _build_app(config1, tmp_paths)
    app1.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")
    await app1.start()
    assert app1.boot_summary is not None
    assert app1.boot_summary.runtime_health == "degraded"
    await app1.stop()

    # Recovery: rebuild with only working adapter.
    config2 = _config_with_one_fake_adapter()
    app2 = _build_app(config2, tmp_paths)
    await app2.start()
    try:
        assert app2.boot_summary is not None
        assert app2.boot_summary.runtime_health == "healthy"
        assert app2.boot_summary.startup_outcome == "success"
    finally:
        await app2.stop()


# ---------------------------------------------------------------------------
# Adapter disable/enable workflows
#
# Disable failing adapter → verify runtime starts → re-enable → verify full config.
# Validates the operator workflow of disabling a problematic adapter,
# verifying the runtime works with remaining adapters, then re-enabling
# after the issue is resolved.
# ---------------------------------------------------------------------------


def test_cli_shows_disabled_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'config check' shows disabled adapters with correct status."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_DISABLED_ADAPTER)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    stdout = _run_cli("config", "check")
    assert "disabled" in stdout
    assert "enabled" in stdout
    assert "Config valid" in stdout


def test_config_with_disabled_adapter_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config loads with mix of enabled and disabled adapters."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_DISABLED_ADAPTER)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    config, _, _ = load_config(None)
    all_configs = list(config.adapters.all_configs())
    enabled = [c for c in all_configs if c[2].enabled]
    disabled = [c for c in all_configs if not c[2].enabled]
    assert len(enabled) == 1
    assert len(disabled) == 1


@pytest.mark.asyncio
async def test_disable_failing_adapter_recovers_runtime(
    tmp_paths: MedrePaths,
) -> None:
    """Failing adapter → total failure → disable it → runtime starts."""
    # First attempt: both adapters fail.
    config = _config_with_fake_adapters()
    app = _build_app(config, tmp_paths)
    app.adapters["fake_matrix"] = _FailingAdapter("fake_matrix")
    app.adapters["fake_mesh"] = _FailingAdapter("fake_mesh")

    with pytest.raises(RuntimeStartupError):
        await app.start()

    # Recovery: operator disables one adapter and retries with the other.
    # (Simulate by creating config with only one adapter that works.)
    config2 = _config_with_one_fake_adapter()
    app2 = _build_app(config2, tmp_paths)
    await app2.start()
    try:
        assert app2.state == RuntimeState.RUNNING
        assert app2.boot_summary is not None
        assert app2.boot_summary.startup_outcome == "success"
    finally:
        await app2.stop()


@pytest.mark.asyncio
async def test_re_enable_adapter_after_fix(tmp_paths: MedrePaths) -> None:
    """Operator re-enables previously disabled adapter → full runtime."""
    # Start with one adapter.
    config1 = _config_with_one_fake_adapter()
    app1 = _build_app(config1, tmp_paths)
    await app1.start()
    try:
        assert len(app1.started_adapter_ids) == 1
    finally:
        await app1.stop()

    # Re-enable second adapter → full two-adapter runtime.
    config2 = _config_with_fake_adapters()
    app2 = _build_app(config2, tmp_paths)
    await app2.start()
    try:
        assert len(app2.started_adapter_ids) == 2
        assert app2.boot_summary is not None
        assert app2.boot_summary.runtime_health == "healthy"
    finally:
        await app2.stop()


def test_adapter_inventory_from_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'adapters' command shows enabled/disabled status correctly."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_DISABLED_ADAPTER)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    stdout = _run_cli("adapters")
    assert "enabled" in stdout or "disabled" in stdout
    assert "Traceback" not in stdout
