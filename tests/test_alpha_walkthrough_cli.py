"""Alpha walkthrough CLI command handler tests.

End-to-end tests for every documented CLI command in
docs/runbooks/alpha-walkthrough.md Section 1.  Exercises CLI command
handlers directly (not the runtime APIs) to prove the operator-facing
command surface works.

Pattern follows tests/test_cli_smoke_run_session.py::test_run_session_persistent_storage.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


class TestAlphaWalkthroughCLI:
    """Prove every documented CLI command in alpha-walkthrough.md Section 1 works."""

    def test_cli_config_check_works(self) -> None:
        """medre config check --config <path> prints 'Config valid'."""
        from medre.cli import main

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            main(["config", "check", "--config", _smoke_config_path()])

        output = stdout.getvalue()
        assert "Config valid" in output

    @pytest.mark.asyncio
    async def test_cli_smoke_inspect_trace_evidence(self, tmp_path: Path) -> None:
        """inspect event, trace event, and evidence handlers produce output after smoke."""
        from medre.runtime.smoke import run_fake_bridge_smoke
        from medre.cli.inspect_commands import _inspect_event
        from medre.cli.trace_commands import _trace_event
        from medre.cli.evidence_commands import _evidence

        # -- Run smoke to populate DB with an event --
        db_path = str(tmp_path / "walkthrough.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # -- Write a minimal config pointing to the DB --
        inspect_config = tmp_path / "walkthrough_config.toml"
        inspect_config.write_text(
            f'[runtime]\nname = "cli-walkthrough"\n\n[storage]\n'
            f'backend = "sqlite"\npath = "{db_path}"\n'
        )
        config_path = str(inspect_config)

        # -- Inspect event --
        inspect_out = io.StringIO()
        with redirect_stdout(inspect_out):
            await _inspect_event(config_path, event_id)
        assert event_id in inspect_out.getvalue()

        # -- Trace event --
        trace_out = io.StringIO()
        with redirect_stdout(trace_out):
            await _trace_event(config_path, event_id, json_output=False)
        assert event_id in trace_out.getvalue()

        # -- Evidence --
        evidence_out = io.StringIO()
        with redirect_stdout(evidence_out):
            await _evidence(
                config_path,
                json_output=False,
                event_id=event_id,
                replay_run_id=None,
                include_refresh_health=False,
            )
        assert len(evidence_out.getvalue().strip()) > 0

    @pytest.mark.asyncio
    async def test_cli_smoke_reports_passed(self, tmp_path: Path) -> None:
        """run_fake_bridge_smoke returns a passed report with expected fields."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "report.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )

        assert report["status"] == "passed", (
            f"Expected PASS, got {report['status']}: "
            f"{report.get('fail_reasons', [])}"
        )
        assert "event_id" in report
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0
        assert len(report["delivery_receipts"]) >= 1
