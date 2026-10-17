"""Guided authoring recovers from answers, not unrelated configuration failures."""

import io
from types import SimpleNamespace

import pytest

from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from tests.helpers.bridge_setup import CONFIG, install_provider
from tests.helpers.cli import _run_cli_raw


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def provider(monkeypatch, tmp_path):
    install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))


def run_guided(tmp_path, monkeypatch, answers, *, document=None, options=()):
    source = tmp_path / "input.yaml"
    source.write_text(dump_yaml_config(document) if document is not None else CONFIG)
    monkeypatch.setattr("sys.stdin", Terminal(answers))
    return _run_cli_raw(
        "config",
        "bridge",
        "--config",
        str(source),
        "--output",
        str(tmp_path / "bridge.yaml"),
        *options,
    )


@pytest.mark.parametrize("problem", ["invalid_id", "unrelated_route"])
def test_unfixable_configuration_fails_before_collecting_contexts(
    tmp_path, monkeypatch, problem
):
    document = parse_yaml_config(CONFIG)
    options = ()
    if problem == "invalid_id":
        options = ("--route-id", "bad.route")
    else:
        document["routes"]["unrelated"] = {
            "source_adapters": ["missing"],
            "dest_adapters": ["right"],
            "enabled": False,
        }
    _, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "left\nright\nbidirectional\nroom-A\nroom-B\n",
        document=document,
        options=options,
    )
    assert code == 2, stderr
    assert "Context rejected" not in stderr
    assert "left context" not in stderr
    assert not (tmp_path / "bridge.yaml").exists()


@pytest.mark.parametrize("answer", ["y\n", "maybe\nyes\n", " Y \n"])
def test_save_confirmation_accepts_yes_and_recovers_from_typos(
    tmp_path, monkeypatch, answer
):
    _, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "left\nright\nbidirectional\nroom-A\nroom-B\n\n\n\n" + answer,
    )
    assert code == 0, stderr
    assert (tmp_path / "bridge.yaml").exists()
    if answer.startswith("maybe"):
        assert "Please answer yes or no" in stderr


def test_reverse_preview_shows_actual_flow_of_conversation_addresses(
    tmp_path, monkeypatch
):
    stdout, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "left\nright\ndest_to_source\nroom-A\nroom-B\n\n\n\nno\n",
    )
    assert code == 0, stderr
    assert "right" in stdout
    assert "context_map: room-B → room-A" in stdout
    assert "context_map: room-A → room-B" not in stdout


def test_real_adapter_elsewhere_does_not_get_unusable_fake_smoke_guidance(
    tmp_path, monkeypatch
):
    document = parse_yaml_config(CONFIG)
    document["adapters"]["independent_chat"]["native"] = {"adapter_kind": "real"}
    _, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "left\nright\nbidirectional\nroom-A\nroom-B\n\n\n\nyes\n",
        document=document,
    )
    assert code == 0, stderr
    assert "medre smoke --config" not in stderr
    assert "medre adapters check --config" in stderr


def test_numbered_selection_handles_a_very_long_invalid_number(tmp_path, monkeypatch):
    _, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "9" * 5000 + "\nleft\nright\nbidirectional\nroom-A\nroom-B\n\n\n\nno\n",
    )
    assert code == 0, stderr
    assert "Invalid source" in stderr


def test_numeric_instance_ids_do_not_shadow_an_explicit_menu_number(
    monkeypatch, capsys
):
    from medre.cli.bridge_commands import _choose

    monkeypatch.setattr("sys.stdin", Terminal("#2\n"))
    assert _choose(None, ["2", "right"], "source", True) == "right"
    assert "#2" in capsys.readouterr().err


def test_windows_next_steps_quote_powershell_metacharacters(monkeypatch):
    from medre.cli import bridge_commands

    monkeypatch.setattr(bridge_commands, "os", SimpleNamespace(name="nt"))
    command = bridge_commands._command_example("medre", "--config", "a&b's $file.yaml")
    assert command == "medre --config 'a&b''s $file.yaml'"


def test_suggested_smoke_keeps_option_like_contexts_as_values(tmp_path, monkeypatch):
    _, stderr, code = run_guided(
        tmp_path,
        monkeypatch,
        "left\nright\nbidirectional\n--room\nroom-B\n\n\n\nyes\n",
    )
    assert code == 0, stderr
    assert "--source-context=--room" in stderr
    stdout, stderr, code = _run_cli_raw(
        "smoke",
        "--config",
        str(tmp_path / "bridge.yaml"),
        "--source-adapter",
        "left",
        "--source-context=--room",
        "--json",
    )
    import json

    assert code == 0, stderr
    assert json.loads(stdout)["source_context"] == "--room"


def test_disabled_text_route_does_not_block_a_new_active_bridge():
    data = parse_yaml_config(CONFIG)
    data["routes"]["unused"] = {
        "source_adapters": ["left"],
        "dest_adapters": ["right"],
        "enabled": False,
        "context_map": {"room-A": {"dest_context": "room-B"}},
    }
    draft = draft_bridge(
        dump_yaml_config(data),
        BridgeRequest("left", "right", (("room-A", "room-B"),)),
        paths=resolve(),
    )
    assert draft.plan.total_legs == 2


def test_source_bounded_route_with_default_target_cannot_double_deliver():
    data = parse_yaml_config(CONFIG)
    data["routes"]["existing"] = {
        "source_adapters": ["left"],
        "dest_adapters": ["right"],
        "source_channel": "room-A",
    }
    with pytest.raises(ConfigValidationError, match="already connects"):
        draft_bridge(
            dump_yaml_config(data),
            BridgeRequest("left", "right", (("room-A", "room-B"),)),
            paths=resolve(),
        )


def test_next_step_command_round_trips_shell_metacharacters():
    import json
    import os
    import shutil
    import subprocess
    import sys

    from medre.cli.bridge_commands import _command_example

    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            pytest.skip("PowerShell is not installed")
        invocation = [shell, "-NoProfile", "-Command"]
    else:
        invocation = ["sh", "-c"]
    arguments = [
        "--config",
        "a&b's $file `x`;quoted.yaml",
        "001",
        "--source-context=--room",
    ]
    command = _command_example(
        sys.executable,
        "-c",
        "import json,sys; print(json.dumps(sys.argv[1:]))",
        *arguments,
    )
    result = subprocess.run(
        [*invocation, command], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == arguments
