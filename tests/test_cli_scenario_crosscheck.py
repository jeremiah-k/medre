"""Scenario cross-check tests — run_bridge_session per scenario.

Every run-session scenario produces correct report fields.  Each scenario
is run via ``run_bridge_session`` with persistent storage and the report is
verified for standardized field shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
        assert (
            report["status"] == "passed"
        ), f"Scenario {scenario} failed: {report.get('fail_reasons', [])}"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_command_is_run_session(
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
    ) -> None:
        """Report commands dict has commands_argv and commands_text with primary/specialized."""
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
        # Each section has primary and specialized sub-dicts.
        for section in ("commands_argv", "commands_text"):
            assert (
                "primary" in commands[section]
            ), f"Missing {section}['primary'] for {scenario}"
            assert (
                "specialized" in commands[section]
            ), f"Missing {section}['specialized'] for {scenario}"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_commands_argv_are_proper_lists(
        self,
        scenario: str,
        tmp_path: Path,
    ) -> None:
        """commands_argv entries are proper lists; read-only use --storage-path, recover uses --config."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"argv-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands_argv = report["commands"]["commands_argv"]
        for category in ("primary", "specialized"):
            cat_cmds = commands_argv[category]
            for cmd_name, argv in cat_cmds.items():
                assert isinstance(argv, list), (
                    f"commands_argv[{category}][{cmd_name!r}] is "
                    f"{type(argv).__name__}, expected list"
                )
                if not argv:
                    continue

                is_config_required = cmd_name == "recover_event"
                if is_config_required:
                    assert "--config" in argv, (
                        f"commands_argv[{category}][{cmd_name!r}] "
                        f"missing --config (config-required): {argv}"
                    )
                else:
                    assert "--storage-path" in argv, (
                        f"commands_argv[{category}][{cmd_name!r}] "
                        f"missing --storage-path (read-only): {argv}"
                    )
                    # The actual DB path must follow --storage-path.
                    sp_idx = argv.index("--storage-path")
                    assert sp_idx + 1 < len(argv), (
                        f"commands_argv[{category}][{cmd_name!r}] "
                        f"--storage-path has no value: {argv}"
                    )
                    assert argv[sp_idx + 1] == db_path, (
                        f"commands_argv[{category}][{cmd_name!r}] "
                        f"--storage-path value != db_path: {argv}"
                    )

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_operator_interpretation_present(
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
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
        self,
        scenario: str,
        tmp_path: Path,
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

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_report_commands_inspect_first(
        self,
        scenario: str,
        tmp_path: Path,
    ) -> None:
        """Report primary commands are inspect-first; specialized are lower-level."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"inspect-first-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands = report.get("commands", {})
        text_commands = commands.get("commands_text", {})
        primary = text_commands.get("primary", {})
        specialized = text_commands.get("specialized", {})

        # Inspect-first primary keys present.
        for key in (
            "inspect_event",
            "inspect_timeline",
            "inspect_receipts",
            "inspect_evidence",
            "inspect_recovery",
        ):
            assert key in primary, f"Missing primary[{key!r}] for {scenario}"
            assert (
                "medre inspect" in primary[key]
            ), f"{key} should contain 'medre inspect': {primary[key]}"

        # Primary commands do NOT start with trace/evidence/recover.
        for key, cmd in primary.items():
            assert not cmd.startswith(
                "medre trace "
            ), f"Primary command {key!r} starts with 'medre trace': {cmd}"
            assert not cmd.startswith(
                "medre evidence "
            ), f"Primary command {key!r} starts with 'medre evidence': {cmd}"
            assert not cmd.startswith(
                "medre recover "
            ), f"Primary command {key!r} starts with 'medre recover': {cmd}"

        # Specialized keys present: trace_event, evidence_bundle, recover_event.
        assert (
            "trace_event" in specialized
        ), f"Missing specialized['trace_event'] for {scenario}"
        assert (
            "evidence_bundle" in specialized
        ), f"Missing specialized['evidence_bundle'] for {scenario}"
        assert (
            "recover_event" in specialized
        ), f"Missing specialized['recover_event'] for {scenario}"

        # Specialized commands use their respective CLI surfaces.
        assert specialized["trace_event"].startswith(
            "medre trace "
        ), f"trace_event should start with 'medre trace': {specialized['trace_event']}"
        assert specialized["evidence_bundle"].startswith("medre evidence "), (
            f"evidence_bundle should start with 'medre evidence': "
            f"{specialized['evidence_bundle']}"
        )
        assert specialized["recover_event"].startswith("medre recover "), (
            f"recover_event should start with 'medre recover': "
            f"{specialized['recover_event']}"
        )


class TestStoragePathInCommands:
    """Verify generated commands point at the runtime storage DB, not only config."""

    @pytest.mark.asyncio
    async def test_primary_commands_use_storage_path_argv(self, tmp_path: Path) -> None:
        """Primary commands_argv use --storage-path with exact DB path when storage is overridden."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "storage-argv.db")
        report = await run_bridge_session(
            scenario="happy_path",
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        commands_argv = report["commands"]["commands_argv"]["primary"]
        for key in (
            "inspect_event",
            "inspect_timeline",
            "inspect_receipts",
            "inspect_evidence",
            "inspect_recovery",
        ):
            argv = commands_argv[key]
            assert (
                "--storage-path" in argv
            ), f"primary[{key}] missing --storage-path: {argv}"
            sp_idx = argv.index("--storage-path")
            assert (
                argv[sp_idx + 1] == db_path
            ), f"primary[{key}] --storage-path value != {db_path}: {argv}"

    @pytest.mark.asyncio
    async def test_specialized_readonly_use_storage_path(self, tmp_path: Path) -> None:
        """Specialized trace/evidence commands use --storage-path."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "spec-ro.db")
        report = await run_bridge_session(
            scenario="happy_path",
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        specialized = report["commands"]["commands_argv"]["specialized"]
        for key in ("trace_event", "evidence_bundle"):
            argv = specialized[key]
            assert (
                "--storage-path" in argv
            ), f"specialized[{key}] missing --storage-path: {argv}"
            sp_idx = argv.index("--storage-path")
            assert argv[sp_idx + 1] == db_path

    @pytest.mark.asyncio
    async def test_recover_remains_config_based(self, tmp_path: Path) -> None:
        """recover_event uses --config, not --storage-path."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "recover-cfg.db")
        report = await run_bridge_session(
            scenario="happy_path",
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        argv = report["commands"]["commands_argv"]["specialized"]["recover_event"]
        assert "--config" in argv, f"recover_event missing --config: {argv}"
        assert (
            "--storage-path" not in argv
        ), f"recover_event should not have --storage-path: {argv}"

    @pytest.mark.asyncio
    async def test_commands_text_shell_safe_spaces(self, tmp_path: Path) -> None:
        """Generated text commands are shell-safe when storage_path has spaces."""
        from medre.runtime.run_session.report import _build_cross_linked_commands

        space_path = "/tmp/path with spaces/session.db"
        config_path = "/tmp/path with spaces/config.toml"

        result = _build_cross_linked_commands(
            event_id="evt_123",
            config_path=config_path,
            snapshot_path=None,
            storage_path=space_path,
        )

        # Verify argv has the raw path (no quoting).
        inspect_argv = result["commands_argv"]["primary"]["inspect_event"]
        assert space_path in inspect_argv

        # Verify text round-trips through shlex.split.
        import shlex

        inspect_text = result["commands_text"]["primary"]["inspect_event"]
        round_tripped = shlex.split(inspect_text)
        assert (
            space_path in round_tripped
        ), f"Path with spaces lost in shell round-trip: {inspect_text}"

        # Verify recover still uses config.
        recover_argv = result["commands_argv"]["specialized"]["recover_event"]
        assert "--config" in recover_argv
        assert "--storage-path" not in recover_argv

        # Text for recover also round-trips correctly.
        recover_text = result["commands_text"]["specialized"]["recover_event"]
        recover_round = shlex.split(recover_text)
        assert config_path in recover_round

    @pytest.mark.asyncio
    async def test_no_storage_path_falls_back_to_config(self) -> None:
        """Without storage_path, read-only commands fall back to --config."""
        from medre.runtime.run_session.report import _build_cross_linked_commands

        result = _build_cross_linked_commands(
            event_id="evt_456",
            config_path="/some/config.toml",
            snapshot_path=None,
            storage_path=None,
        )

        inspect_argv = result["commands_argv"]["primary"]["inspect_event"]
        assert "--config" in inspect_argv
        assert "--storage-path" not in inspect_argv

    @pytest.mark.asyncio
    async def test_inspect_argv_executable_via_cli(self, tmp_path: Path) -> None:
        """Generated inspect argv can be executed through CLI _inspect_event against the actual DB."""
        import io
        from contextlib import redirect_stdout

        from medre.cli.inspect_commands import _inspect_event
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "cli-exec.db")
        report = await run_bridge_session(
            scenario="happy_path",
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        event_id = report["event_id"]
        inspect_argv = report["commands"]["commands_argv"]["primary"]["inspect_event"]

        # Extract storage_path from the generated argv.
        sp_idx = inspect_argv.index("--storage-path")
        extracted_db_path = inspect_argv[sp_idx + 1]
        assert extracted_db_path == db_path

        # Execute the underlying inspect function directly (asyncio.run in
        # main() conflicts with pytest-asyncio's running loop).
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            await _inspect_event(
                config_path=None,
                event_id=event_id,
                storage_path=extracted_db_path,
            )

        output = stdout_buf.getvalue()
        assert (
            event_id in output
        ), f"Event ID {event_id!r} not in inspect output: {output[:500]}"
