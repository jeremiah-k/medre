"""Scenario cross-check tests — run_bridge_session per scenario.

Every run-session scenario produces correct report fields.  Each scenario
is run via ``run_bridge_session`` with persistent storage and the report is
verified for standardized field shapes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_cli_config_workflows import _run_cli


_SCENARIOS = (
    "happy_path",
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "degraded_live_health",
)

_FAILURE_SCENARIOS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
)

_DELIVERY_FAILURE_SCENARIOS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
)


class TestScenarioCrossCheck:
    """Every run-session scenario produces correct report fields."""

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_status_is_passed(self, scenario: str, tmp_path: Path) -> None:
        """Every scenario produces status='passed'."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"scenario-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed", (
            f"Scenario {scenario} failed: {report.get('fail_reasons', [])}"
        )

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_command_is_run_session(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has command='run_session'."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"cmd-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("command") == "run_session"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_scenario_category_matches(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report scenario_category matches expected category."""
        from medre.runtime.run_session import (
            run_bridge_session,
            scenario_category,
        )

        db_path = str(tmp_path / f"cat-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        expected_cat = scenario_category(scenario)
        assert report.get("scenario_category") == expected_cat, (
            f"scenario_category for {scenario}: "
            f"expected {expected_cat!r}, got {report.get('scenario_category')!r}"
        )

    @pytest.mark.parametrize("scenario", _FAILURE_SCENARIOS)
    @pytest.mark.asyncio
    async def test_failure_scenario_simulated(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Failure scenarios have simulated=True and simulation_method present."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"sim-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("simulated") is True
        assert report.get("simulation_method") is not None
        assert isinstance(report["simulation_method"], str)

    @pytest.mark.asyncio
    async def test_degraded_health_fields(self, tmp_path: Path) -> None:
        """degraded_live_health scenario has expected/observed health fields."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "degraded-health.db")
        report = await run_bridge_session(
            scenario="degraded_live_health",
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("expected_health") == "degraded"
        assert report.get("observed_health") == "degraded"
        assert report.get("expected_failure_kind") is None

    @pytest.mark.parametrize("scenario", _DELIVERY_FAILURE_SCENARIOS)
    @pytest.mark.asyncio
    async def test_delivery_failure_has_failure_kinds(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Delivery failure scenarios have expected/observed failure_kind."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"fk-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("expected_failure_kind") is not None
        assert report.get("observed_failure_kind") is not None
        assert report["expected_failure_kind"] == report["observed_failure_kind"]

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_commands_dict_has_argv_and_text(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report commands dict has commands_argv (list) and commands_text (string)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"cmds-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands = report.get("commands", {})
        assert "commands_argv" in commands, f"Missing commands_argv for {scenario}"
        assert "commands_text" in commands, f"Missing commands_text for {scenario}"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_commands_argv_are_proper_lists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """commands_argv entries are proper lists (not strings) containing config path."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"argv-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands_argv = report["commands"]["commands_argv"]
        for cmd_name, argv in commands_argv.items():
            assert isinstance(argv, list), (
                f"commands_argv[{cmd_name!r}] is {type(argv).__name__}, expected list"
            )
            if argv:
                assert "--config" in argv, (
                    f"commands_argv[{cmd_name!r}] missing --config: {argv}"
                )

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_operator_interpretation_present(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has operator_interpretation (non-empty)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"interp-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        interp = report.get("operator_interpretation")
        assert interp is not None, f"Missing operator_interpretation for {scenario}"
        assert isinstance(interp, str)
        assert len(interp) > 0

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_errors_list_exists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has 'errors' list (may be empty)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"errors-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert "errors" in report
        assert isinstance(report["errors"], list)

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_limitations_list_exists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has 'limitations' list."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"lim-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert "limitations" in report
        assert isinstance(report["limitations"], list)
        assert len(report["limitations"]) > 0
