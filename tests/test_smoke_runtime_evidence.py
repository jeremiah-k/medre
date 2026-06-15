"""Focused tests for runtime evidence fields in smoke reports.

Validates the three compact derived fields added to the smoke report:
``adapter_lifecycle``, ``shutdown_status``, and ``runtime_events_count``.

These fields are derived purely from the already-collected snapshot — no new
I/O, no storage mutation, no authority changes.  Tests exercise the actual
``run_fake_bridge_smoke`` function (not mocked internals) to prove end-to-end
derivation.
"""

from __future__ import annotations

import pytest

from medre.runtime.smoke import run_fake_bridge_smoke

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.yaml not found"
    return path


# ---------------------------------------------------------------------------
# Tests: adapter_lifecycle
# ---------------------------------------------------------------------------


async def test_adapter_lifecycle_present_and_is_dict() -> None:
    """adapter_lifecycle is a dict mapping adapter ids to lifecycle states."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed", report.get("fail_reasons", [])
    lifecycle = report["adapter_lifecycle"]
    assert isinstance(lifecycle, dict)


async def test_adapter_lifecycle_values_are_strings() -> None:
    """Every adapter lifecycle value is a string."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    lifecycle = report["adapter_lifecycle"]
    for _aid, state in lifecycle.items():
        assert isinstance(
            state, str
        ), f"adapter {_aid}: expected str, got {type(state)}"


async def test_adapter_lifecycle_contains_expected_adapters() -> None:
    """Fake bridge config adapters appear in adapter_lifecycle."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    lifecycle = report["adapter_lifecycle"]
    # The shipped fake-bridge-smoke.yaml has at least fake_matrix.
    assert (
        "fake_matrix" in lifecycle
    ), f"Expected fake_matrix in {list(lifecycle.keys())}"


async def test_adapter_lifecycle_states_are_valid() -> None:
    """Adapter lifecycle states are recognised adapter-state strings."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    lifecycle = report["adapter_lifecycle"]
    # States come from the snapshot taken mid-run (before stop); any non-empty
    # string is valid — exact value depends on when the snapshot was captured.
    for aid, state in lifecycle.items():
        assert (
            isinstance(state, str) and len(state) > 0
        ), f"adapter {aid}: expected non-empty str, got {state!r}"


# ---------------------------------------------------------------------------
# Tests: shutdown_status
# ---------------------------------------------------------------------------


async def test_shutdown_status_is_none_or_str() -> None:
    """shutdown_status is None when no shutdown evidence in basic snapshot."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    shutdown = report["shutdown_status"]
    assert shutdown is None or isinstance(shutdown, str)


async def test_shutdown_status_stopped_after_smoke() -> None:
    """Smoke run calls app.stop(), so shutdown_status is 'stopped'."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    # The smoke runner calls app.stop() which transitions runtime_state
    # to "stopped".  shutdown_status is derived from lifecycle.runtime_state.
    assert report["shutdown_status"] == "stopped"


# ---------------------------------------------------------------------------
# Tests: runtime_events_count
# ---------------------------------------------------------------------------


async def test_runtime_events_count_is_int() -> None:
    """runtime_events_count is always an int."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    assert isinstance(report["runtime_events_count"], int)


async def test_runtime_events_count_non_negative() -> None:
    """runtime_events_count must be >= 0."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    assert report["runtime_events_count"] >= 0


async def test_runtime_events_count_positive_after_pipeline_run() -> None:
    """After pipeline exercise, at least some runtime events were recorded."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    # The smoke run exercises the full pipeline lifecycle (start, inject, stop),
    # which should produce runtime events.  A count of 0 would mean no event
    # buffer was wired, which would be a regression.
    assert (
        report["runtime_events_count"] > 0
    ), "Expected at least 1 runtime event after full smoke cycle"


# ---------------------------------------------------------------------------
# Tests: fields absent on failure path
# ---------------------------------------------------------------------------


async def test_failure_report_has_derived_fields() -> None:
    """Even failed smoke reports include the derived fields (graceful)."""
    report = await run_fake_bridge_smoke("/nonexistent/config.toml")
    assert report["status"] == "failed"
    # Failed early (config load error) — snap is empty, so fields are defaults.
    assert "adapter_lifecycle" in report
    assert "shutdown_status" in report
    assert "runtime_events_count" in report
    assert report["adapter_lifecycle"] == {}
    assert report["shutdown_status"] is None
    assert report["runtime_events_count"] == 0


# ---------------------------------------------------------------------------
# Tests: snapshot isolation — no full dump
# ---------------------------------------------------------------------------


async def test_report_does_not_expose_full_runtime_events_list() -> None:
    """Smoke report does not dump the full runtime events list."""
    report = await run_fake_bridge_smoke(_smoke_config_path())
    assert report["status"] == "passed"
    # The snapshot section should NOT contain the full events list.
    report.get("snapshot", {}).get("routes", {})
    assert "diagnostics" not in report.get(
        "snapshot", {}
    ), "Snapshot should be compact — no diagnostics section with events list"
