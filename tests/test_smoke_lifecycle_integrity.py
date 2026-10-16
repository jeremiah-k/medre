# medre-test-tier: compatibility
"""Diagnostic failures and cancellation never prove a clean lifecycle."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from medre.runtime import smoke
from medre.runtime.app import MedreApp, RuntimeState
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.run_session import orchestration


@pytest.mark.parametrize("runner", [smoke, orchestration])
async def test_fake_diagnostics_reject_live_targets_before_build(
    tmp_path, monkeypatch, runner
):
    config = yaml.safe_load(Path(smoke._default_smoke_config_path()).read_text())
    config["adapters"]["meshcore"]["fake_meshcore"]["adapter_kind"] = "real"
    path = tmp_path / "real.yaml"
    path.write_text(yaml.safe_dump(config))
    build = Mock(side_effect=AssertionError("must not build real adapters"))
    monkeypatch.setattr(RuntimeBuilder, "build", build)
    run = (
        smoke.run_fake_bridge_smoke
        if runner is smoke
        else orchestration.run_bridge_session
    )
    report = await run(
        config_path=str(path), storage_path=str(tmp_path / "session.sqlite")
    )
    assert report["status"] == "failed"
    assert "requires adapter_kind: fake" in report["fail_reason"]
    build.assert_not_called()


@pytest.mark.parametrize("runner", [smoke, orchestration])
@pytest.mark.parametrize("fault", ["cancel", "routes", "stop"])
async def test_runtime_is_closed_without_masking_failure(
    tmp_path, monkeypatch, runner, fault
):
    apps = []
    original_build = RuntimeBuilder.build
    original_stop = MedreApp.stop

    def build(self):
        app = original_build(self)
        apps.append(app)
        return app

    monkeypatch.setattr(RuntimeBuilder, "build", build)
    if fault == "stop":

        async def stop(self, *args, **kwargs):
            await original_stop(self, *args, **kwargs)
            raise RuntimeError("shutdown evidence fault")

        monkeypatch.setattr(MedreApp, "stop", stop)
    else:

        def fail(*args, **kwargs):
            if fault == "cancel":
                raise asyncio.CancelledError()
            raise ValueError("route evidence fault")

        monkeypatch.setattr(
            runner,
            "make_smoke_event" if fault == "cancel" else "expected_smoke_deliveries",
            fail,
        )
    run = (
        smoke.run_fake_bridge_smoke
        if runner is smoke
        else orchestration.run_bridge_session
    )
    kwargs = dict(
        config_path=smoke._default_smoke_config_path(),
        storage_path=str(tmp_path / "session.sqlite"),
    )
    if fault == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run(**kwargs)
    else:
        report = await run(**kwargs)
        assert report["status"] == "failed", report
        assert any("fault" in reason for reason in report["fail_reasons"])
    assert apps[0].state == RuntimeState.STOPPED


@pytest.mark.parametrize("scenario", ["happy_path", "renderer_failure"])
async def test_session_snapshot_failure_cannot_pass(tmp_path, monkeypatch, scenario):
    def fail(*args, **kwargs):
        raise ValueError("snapshot unavailable")

    monkeypatch.setattr(orchestration, "build_runtime_snapshot", fail)
    report = await orchestration.run_bridge_session(
        config_path=smoke._default_smoke_config_path(),
        storage_path=str(tmp_path / "session.sqlite"),
        scenario=scenario,
    )
    assert report["status"] == "failed", report
    assert any("snapshot unavailable" in reason for reason in report["fail_reasons"])


async def test_session_accounting_counts_route_target_pairs(tmp_path, monkeypatch):
    from medre.core.supervision.accounting import RuntimeAccounting

    original = RuntimeAccounting.snapshot

    def undercount(self):
        return {**original(self), "outbound_delivered": 1}

    monkeypatch.setattr(RuntimeAccounting, "snapshot", undercount)
    report = await orchestration.run_bridge_session(
        config_path=smoke._default_smoke_config_path(),
        storage_path=str(tmp_path / "session.sqlite"),
    )
    assert report["status"] == "failed", report
    assert any(
        "configured delivery count" in reason for reason in report["fail_reasons"]
    )
