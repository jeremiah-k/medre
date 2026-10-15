# medre-test-tier: compatibility
"""Run-session exercises configured source routes in both ingress modes."""

from pathlib import Path

import pytest
import yaml

from medre.runtime.run_session.orchestration import run_bridge_session
from medre.runtime.smoke import _default_smoke_config_path


@pytest.mark.parametrize("mode", ["direct_pipeline", "adapter_callback"])
async def test_target_only_matrix_is_never_selected_as_source(
    tmp_path: Path, mode: str
):
    config = yaml.safe_load(Path(_default_smoke_config_path()).read_text())
    config["routes"] = {
        "mesh_to_targets": {
            "source_adapters": ["fake_meshtastic"],
            "dest_adapters": ["fake_matrix", "fake_meshcore"],
            "enabled": True,
        }
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    report = await run_bridge_session(
        config_path=str(path),
        storage_path=str(tmp_path / "session.sqlite"),
        ingress_mode=mode,
    )
    assert report["status"] == "passed", report.get("fail_reasons")
    assert report["source_adapter"] == "fake_meshtastic"
    assert report["shutdown_status"] == "stopped"


@pytest.mark.parametrize("hook", ["pick_source_adapter", "make_smoke_event"])
async def test_session_hook_errors_return_failure_and_close_runtime(
    tmp_path, monkeypatch, hook
):
    from medre.runtime.app import RuntimeState
    from medre.runtime.run_session import orchestration

    apps = []
    original = orchestration.RuntimeBuilder.build

    def build(builder):
        app = original(builder)
        apps.append(app)
        return app

    def fail(*args, **kwargs):
        raise ValueError("unsupported fake ingress")

    monkeypatch.setattr(orchestration.RuntimeBuilder, "build", build)
    monkeypatch.setattr(orchestration, hook, fail)
    report = await orchestration.run_bridge_session(
        config_path=_default_smoke_config_path(),
        storage_path=str(tmp_path / "session.sqlite"),
    )
    assert report["status"] == "failed"
    assert "unsupported fake ingress" in report["fail_reasons"][0]
    assert apps[0].state == RuntimeState.STOPPED


async def test_session_cli_prints_early_failure_reason(tmp_path, monkeypatch, capsys):
    from unittest.mock import AsyncMock

    from medre.cli.smoke_commands import _run_session
    from medre.runtime.run_session import orchestration

    monkeypatch.setattr(
        orchestration,
        "run_bridge_session",
        AsyncMock(
            return_value={"status": "failed", "fail_reason": "Missing provider details"}
        ),
    )
    with pytest.raises(SystemExit):
        await _run_session(
            None, None, False, storage_path=str(tmp_path / "session.sqlite")
        )
    assert "Missing provider details" in capsys.readouterr().out
