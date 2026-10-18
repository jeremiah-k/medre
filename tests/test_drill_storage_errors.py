# medre-test-tier: compatibility
"""Drill storage-read failures surface as storage_error verdicts.

Proves that:

- A broken ``list_receipts_for_event`` read yields a ``"storage_error"``
  step verdict carrying the error text, never a false missing-receipt or
  zero-count verdict.
- The drill report fails instead of passing on unreadable evidence.
- Healthy storage keeps the established verdicts and report shapes.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.drill import run_drill


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
    return str(path)


def _find_step(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in report["drill_steps"] if s["step"] == name)


def _patch_broken_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every receipt read raise a storage fault."""

    async def broken(*args: Any, **kwargs: Any) -> list[Any]:
        raise RuntimeError("storage read fault")

    monkeypatch.setattr(SQLiteStorage, "list_receipts_for_event", broken)


class TestRendererFailureStorageError:
    """Broken receipt reads cannot masquerade as a missing failed receipt."""

    @pytest.mark.asyncio
    async def test_receipt_read_failure_reports_storage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_broken_read(monkeypatch)
        report = await run_drill("renderer_failure", config_path=_smoke_config_path())
        assert report["status"] == "failed"
        assert any("storage read fault" in reason for reason in report["fail_reasons"])
        step = _find_step(report, "verify_failed_receipt")
        assert step["result"] == "storage_error"
        assert "storage read fault" in step["storage_error"]
        assert "has_failed_receipt" not in step
        json.dumps(report, sort_keys=True)

    @pytest.mark.asyncio
    async def test_healthy_read_keeps_receipt_verdict(self) -> None:
        report = await run_drill("renderer_failure", config_path=_smoke_config_path())
        assert report["status"] == "passed", report.get("fail_reasons", [])
        step = _find_step(report, "verify_failed_receipt")
        assert step["result"] == "ok"
        assert step["has_failed_receipt"] is True
        assert "storage_error" not in step


class TestCapacityRejectionStorageError:
    """Broken receipt reads cannot masquerade as suppression verdicts."""

    @pytest.mark.asyncio
    async def test_receipt_read_failure_reports_storage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_broken_read(monkeypatch)
        report = await run_drill("capacity_rejection", config_path=_smoke_config_path())
        assert report["status"] == "failed"
        assert any("storage read fault" in reason for reason in report["fail_reasons"])
        step = _find_step(report, "verify_suppressed_receipts")
        assert step["result"] == "storage_error"
        assert "storage read fault" in step["storage_error"]
        assert "receipt_count" not in step
        assert "all_suppressed" not in step
        json.dumps(report, sort_keys=True)

    @pytest.mark.asyncio
    async def test_healthy_read_keeps_suppression_verdict(self) -> None:
        report = await run_drill("capacity_rejection", config_path=_smoke_config_path())
        assert report["status"] == "passed", report.get("fail_reasons", [])
        step = _find_step(report, "verify_suppressed_receipts")
        assert step["result"] == "ok"
        assert step["all_suppressed"] is True
        assert "storage_error" not in step


class TestShutdownRejectionStorageError:
    """Broken receipt reads cannot masquerade as suppression verdicts."""

    @pytest.mark.asyncio
    async def test_receipt_read_failure_reports_storage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_broken_read(monkeypatch)
        report = await run_drill("shutdown_rejection", config_path=_smoke_config_path())
        assert report["status"] == "failed"
        assert any("storage read fault" in reason for reason in report["fail_reasons"])
        step = _find_step(report, "verify_suppressed_receipts")
        assert step["result"] == "storage_error"
        assert "storage read fault" in step["storage_error"]
        assert "receipt_count" not in step
        # The suppression comparison does not run, so no fake timeline counts.
        assert "rejection_timeline" not in report
        json.dumps(report, sort_keys=True)

    @pytest.mark.asyncio
    async def test_healthy_read_keeps_suppression_verdict(self) -> None:
        report = await run_drill("shutdown_rejection", config_path=_smoke_config_path())
        assert report["status"] == "passed", report.get("fail_reasons", [])
        step = _find_step(report, "verify_suppressed_receipts")
        assert step["result"] == "ok"
        assert step["all_suppressed"] is True
        assert report["rejection_timeline"]["suppressed_receipts_created"] >= 1
        assert "storage_error" not in step


class TestReplayDuplicateRiskStorageError:
    """Broken receipt reads cannot masquerade as count-based verdicts."""

    @pytest.mark.asyncio
    async def test_read_failures_report_storage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_broken_read(monkeypatch)
        report = await run_drill(
            "replay_duplicate_risk", config_path=_smoke_config_path()
        )
        assert report["status"] == "failed"
        assert any("storage read fault" in reason for reason in report["fail_reasons"])
        step = _find_step(report, "verify_duplicate_receipts")
        assert step["result"] == "storage_error"
        assert "storage read fault" in step["storage_error"]
        assert "new_receipts" not in step
        # The count comparison does not run, so no fake timeline is emitted.
        assert "receipt_timeline" not in report
        json.dumps(report, sort_keys=True)

    @pytest.mark.asyncio
    async def test_post_replay_read_failure_reports_storage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-replay read succeeds; the post-replay read fails."""
        original = SQLiteStorage.list_receipts_for_event
        calls = {"count": 0}

        async def flaky(*args: Any, **kwargs: Any) -> list[Any]:
            calls["count"] += 1
            if calls["count"] == 1:
                return await original(*args, **kwargs)
            raise RuntimeError("storage read fault")

        monkeypatch.setattr(SQLiteStorage, "list_receipts_for_event", flaky)
        report = await run_drill(
            "replay_duplicate_risk", config_path=_smoke_config_path()
        )
        assert report["status"] == "failed"
        assert any("storage read fault" in reason for reason in report["fail_reasons"])
        step = _find_step(report, "verify_duplicate_receipts")
        assert step["result"] == "storage_error"
        assert "storage read fault" in step["storage_error"]
        assert "receipt_timeline" not in report

    @pytest.mark.asyncio
    async def test_healthy_read_keeps_count_verdict(self) -> None:
        report = await run_drill(
            "replay_duplicate_risk", config_path=_smoke_config_path()
        )
        assert report["status"] == "passed", report.get("fail_reasons", [])
        step = _find_step(report, "verify_duplicate_receipts")
        assert step["result"] == "ok"
        assert step["new_receipts"] > 0
        assert report["receipt_timeline"]["timeline_verified"] is True
        assert "storage_error" not in step


@pytest.mark.parametrize(
    "name",
    [
        "renderer_failure",
        "capacity_rejection",
        "shutdown_rejection",
        "replay_duplicate_risk",
    ],
)
async def test_storage_step_details_redact_backend_credentials(monkeypatch, name):
    async def broken(*args, **kwargs):
        raise RuntimeError(
            "storage read fault password=private-password token=private-token"
        )

    monkeypatch.setattr(SQLiteStorage, "list_receipts_for_event", broken)
    report = await run_drill(name, config_path=_smoke_config_path())
    from pathlib import Path

    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(
        json.loads(Path("docs/schemas/drill-step.schema.json").read_text())
    )
    for step in report["drill_steps"]:
        validator.validate(step)
    serialized = json.dumps(report)
    assert "private-password" not in serialized
    assert "private-token" not in serialized
    assert "storage read fault" in serialized
    assert report["status"] == "failed"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("capacity_rejection", "CAPACITY_REJECTION"),
        ("shutdown_rejection", "SHUTDOWN_REJECTION"),
    ],
)
async def test_storage_fault_survives_a_simultaneous_missing_outcome(
    monkeypatch, name, expected
):
    from medre.core.engine.pipeline.runner import PipelineRunner

    async def no_outcomes(self, event):
        return []

    monkeypatch.setattr(PipelineRunner, "handle_ingress", no_outcomes)
    _patch_broken_read(monkeypatch)
    report = await run_drill(name, config_path=_smoke_config_path())
    assert any("storage read fault" in reason for reason in report["fail_reasons"])
    assert any(expected in reason for reason in report["fail_reasons"])


async def test_unreadable_replay_baseline_never_claims_zero_receipts(monkeypatch):
    _patch_broken_read(monkeypatch)
    report = await run_drill("replay_duplicate_risk", config_path=_smoke_config_path())
    step = _find_step(report, "first_delivery")
    assert step["result"] == "storage_error"
    assert "receipt_count" not in step
    assert not any(
        step["step"] == "replay_best_effort" for step in report["drill_steps"]
    )
