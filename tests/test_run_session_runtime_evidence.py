"""Focused tests for runtime evidence enrichment in run-session reports.

Tests the three compact runtime evidence fields added to run-session reports:
  - ``adapter_lifecycle``: per-adapter lifecycle state from snapshot
  - ``shutdown_status``: ``"stopped"`` when shutdown evidence present, else ``None``
  - ``retry_worker_summary``: compact retry worker counters from snapshot

All fields are derived from already-collected snapshot data — no new I/O,
no storage mutation, no execution behavior changes.
"""

from __future__ import annotations

import json
from pathlib import Path


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.yaml not found"
    return path


# ---------------------------------------------------------------------------
# Happy-path evidence enrichment
# ---------------------------------------------------------------------------


async def test_adapter_lifecycle_populated_after_stop(tmp_path: Path) -> None:
    """adapter_lifecycle is a dict[str, str] with adapter IDs as keys."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    db_path = str(tmp_path / "evidence.db")
    report = await run_bridge_session(
        config_path=_smoke_config_path(),
        storage_path=db_path,
    )
    assert report["status"] == "passed", report.get("fail_reasons", [])

    adapter_lifecycle = report["adapter_lifecycle"]
    assert isinstance(adapter_lifecycle, dict)
    assert len(adapter_lifecycle) > 0, "Expected at least one adapter lifecycle entry"

    for aid, state in adapter_lifecycle.items():
        assert isinstance(aid, str), f"Adapter key {aid!r} is not str"
        assert isinstance(state, str), f"State for {aid!r} is not str: {state!r}"


async def test_shutdown_status_stopped_after_clean_stop(tmp_path: Path) -> None:
    """shutdown_status is 'stopped' after a successful run-session."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    db_path = str(tmp_path / "shutdown.db")
    report = await run_bridge_session(
        config_path=_smoke_config_path(),
        storage_path=db_path,
    )
    assert report["status"] == "passed"

    assert report["shutdown_status"] == "stopped"


async def test_retry_worker_summary_compact_shape(tmp_path: Path) -> None:
    """retry_worker_summary has the 6 compact fields with correct types."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    db_path = str(tmp_path / "retry.db")
    report = await run_bridge_session(
        config_path=_smoke_config_path(),
        storage_path=db_path,
    )
    assert report["status"] == "passed"

    rws = report["retry_worker_summary"]
    assert isinstance(rws, dict)

    expected_keys = {
        "enabled",
        "running",
        "processed",
        "succeeded",
        "failed",
        "dead_lettered",
    }
    assert (
        set(rws.keys()) == expected_keys
    ), f"Unexpected keys: {set(rws.keys()) - expected_keys}"

    # Boolean fields
    assert isinstance(rws["enabled"], bool)
    assert isinstance(rws["running"], bool)

    # Integer fields
    for int_key in ("processed", "succeeded", "failed", "dead_lettered"):
        assert isinstance(
            rws[int_key], int
        ), f"retry_worker_summary[{int_key!r}] is {type(rws[int_key]).__name__}, expected int"
        assert rws[int_key] >= 0


async def test_runtime_evidence_json_safe(tmp_path: Path) -> None:
    """All three evidence fields survive JSON round-trip."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    db_path = str(tmp_path / "json.db")
    report = await run_bridge_session(
        config_path=_smoke_config_path(),
        storage_path=db_path,
    )
    assert report["status"] == "passed"

    serialized = json.dumps(report, sort_keys=True)
    parsed = json.loads(serialized)

    assert isinstance(parsed["adapter_lifecycle"], dict)
    assert parsed["shutdown_status"] == "stopped"
    assert isinstance(parsed["retry_worker_summary"], dict)
    assert set(parsed["retry_worker_summary"].keys()) == {
        "enabled",
        "running",
        "processed",
        "succeeded",
        "failed",
        "dead_lettered",
    }


# ---------------------------------------------------------------------------
# Early-failure paths: snap is empty → evidence fields degrade gracefully
# ---------------------------------------------------------------------------


async def test_adapter_lifecycle_empty_on_config_failure(tmp_path: Path) -> None:
    """When config load fails, adapter_lifecycle is empty dict."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    report = await run_bridge_session(
        config_path=str(tmp_path / "nonexistent.yaml"),
        storage_path=str(tmp_path / "noop.db"),
    )
    assert report["status"] == "failed"
    assert isinstance(report["adapter_lifecycle"], dict)
    assert len(report["adapter_lifecycle"]) == 0


async def test_shutdown_status_none_on_config_failure(tmp_path: Path) -> None:
    """When config load fails (no snapshot), shutdown_status is None."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    report = await run_bridge_session(
        config_path=str(tmp_path / "nonexistent.yaml"),
        storage_path=str(tmp_path / "noop.db"),
    )
    assert report["status"] == "failed"
    assert report["shutdown_status"] is None


async def test_retry_worker_summary_none_on_config_failure(tmp_path: Path) -> None:
    """When config load fails (no snapshot), retry_worker_summary is None."""
    from medre.runtime.run_session.orchestration import run_bridge_session

    report = await run_bridge_session(
        config_path=str(tmp_path / "nonexistent.yaml"),
        storage_path=str(tmp_path / "noop.db"),
    )
    assert report["status"] == "failed"
    assert report["retry_worker_summary"] is None
