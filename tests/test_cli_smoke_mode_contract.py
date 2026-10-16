"""Smoke options must not silently select a different diagnostic mode."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from medre.cli import main
from medre.cli.main import _build_parser
from tests.helpers.cli import _run_cli_raw


@pytest.mark.parametrize(
    ("options", "explanation"),
    [
        (("--drill", "renderer_failure", "--run-session"), "not allowed"),
        (("--run-session", "--drill", "renderer_failure"), "not allowed"),
        (("--scenario", "renderer_failure"), "--scenario requires --run-session"),
        (("--snapshot-dir", "snapshots"), "--snapshot-dir requires --run-session"),
        (
            ("--drill", "renderer_failure", "--scenario", "happy_path"),
            "--scenario requires --run-session",
        ),
        (
            ("--drill", "renderer_failure", "--snapshot-dir", "snapshots"),
            "--snapshot-dir requires --run-session",
        ),
        (
            ("--drill", "renderer_failure", "--message", "custom"),
            "--message is supported only for normal smoke",
        ),
        (
            ("--run-session", "--message", "custom"),
            "--message is supported only for normal smoke",
        ),
        (
            ("--run-session", "--source-adapter", "source"),
            "--source-adapter is supported only for normal smoke",
        ),
    ],
)
def test_incompatible_modes_fail_before_runtime_work(monkeypatch, options, explanation):
    build = Mock(side_effect=AssertionError("runtime must not start"))
    monkeypatch.setattr("medre.runtime.builder.RuntimeBuilder.build", build)
    stdout, stderr, status = _run_cli_raw("smoke", *options)
    assert status == 2
    assert stdout == ""
    assert explanation in stderr
    build.assert_not_called()


def test_default_message_is_retained_for_normal_smoke():
    args = _build_parser().parse_args(["smoke"])
    assert args.message is None
    assert args.drill is None
    assert args.run_session is False
    assert args.scenario is None


@pytest.mark.parametrize("message", ["", "explicit message"])
def test_normal_smoke_preserves_explicit_message(monkeypatch, message):
    observed = []

    async def fake_smoke(*args, **kwargs):
        observed.append((args, kwargs))

    monkeypatch.setattr("medre.cli.smoke_commands._smoke", fake_smoke)
    main(["smoke", "--message", message])
    assert observed[0][0][1] == message
    main(["smoke"])
    assert observed[1][0][1] == "medre smoke test"


@pytest.mark.parametrize("scenario", [None, "renderer_failure"])
def test_run_session_keeps_default_and_explicit_scenario(monkeypatch, scenario):
    observed = []

    async def fake_session(*args, **kwargs):
        observed.append(kwargs)

    monkeypatch.setattr("medre.cli.smoke_commands._run_session", fake_session)
    argv = ["smoke", "--run-session"]
    if scenario is not None:
        argv.extend(["--scenario", scenario])
    main(argv)
    assert observed[0]["scenario"] == (scenario or "happy_path")


def test_operator_scenario_example_selects_run_session():
    guide = (
        Path(__file__).resolve().parents[1] / "docs/ops/diagnostics-and-evidence.md"
    ).read_text(encoding="utf-8")
    assert "medre smoke --run-session --scenario <name> --json" in guide
    assert "medre smoke --scenario <name> --json" not in guide
