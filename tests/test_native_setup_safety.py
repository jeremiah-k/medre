"""Failure contracts for provider prompts and bounded route revision."""

import io
from argparse import Namespace
from dataclasses import replace
import warnings
from unittest.mock import patch

import pytest

from medre import adapter_registry
from medre.adapter_spec import NativeSetupField
from medre.cli import native_commands
from medre.config._yaml import dump_yaml_config, parse_yaml_config
from medre.config.errors import ConfigValidationError
from medre.config.loader import parse_runtime_config
from medre.config.paths import resolve
from medre.runtime.bridge_setup import BridgeRequest, draft_bridge
from medre.runtime.route_explain import explain_routes
from tests.helpers.bridge_setup import CONFIG, install_provider


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def provider(monkeypatch, tmp_path):
    spec = replace(
        install_provider(monkeypatch),
        native_setup_fields=(
            NativeSetupField("credential", "Credential", secret=True),
            NativeSetupField("origin_label", "Origin"),
        ),
    )
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        adapter_registry.AdapterTypeRegistry((spec,)),
    )
    return spec


@pytest.mark.parametrize(
    "field",
    [
        NativeSetupField("credential", "Visible secret"),
        NativeSetupField("credential", "Secret", secret=True, choices=("leak",)),
        NativeSetupField("origin_label", "Label", required="yes"),
        NativeSetupField("origin_label", "Label", choices=(1,)),
        NativeSetupField("origin_label", "Label", active_when=("credential",)),
        NativeSetupField(
            "origin_label", "Label", active_when=("credential", "not-a-choice")
        ),
    ],
)
def test_malformed_or_visible_secret_prompts_are_rejected(provider, field):
    with pytest.raises(ConfigValidationError):
        native_commands._validate_native_fields(
            replace(provider, native_setup_fields=(field,))
        )


def test_prompt_failure_redacts_secrets_entered_before_failure(
    provider, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", Terminal(""))
    output = tmp_path / "native.yaml"
    with (
        patch.object(
            native_commands.getpass, "getpass", return_value="secret-before-failure"
        ),
        patch.object(
            native_commands, "_ask", side_effect=ValueError("secret-before-failure")
        ),
        pytest.raises(SystemExit) as error,
    ):
        native_commands._config_native(
            Namespace(
                transport=provider.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    assert error.value.code == 2
    assert "secret-before-failure" not in capsys.readouterr().err
    assert not output.exists()


def test_hidden_input_failure_cannot_fall_back_to_echo(
    provider, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.stdin", Terminal(""))
    output = tmp_path / "native.yaml"

    def no_echo_control(*args):
        warnings.warn(
            "Password input may be echoed",
            native_commands.getpass.GetPassWarning,
            stacklevel=2,
        )
        pytest.fail("Prompt continued after echo control failed")

    with (
        patch.object(native_commands.getpass, "getpass", side_effect=no_echo_control),
        pytest.raises(SystemExit) as error,
    ):
        native_commands._config_native(
            Namespace(
                transport=provider.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    assert error.value.code == 2
    assert "Hidden input is unavailable" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"source_origin_label": "per-context identity"},
        {"dest_origin_label": "reverse identity"},
    ],
)
def test_route_replacement_refuses_context_attribution(provider, extra):
    draft = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "topic"),)), paths=resolve()
    )
    raw = parse_yaml_config(draft.yaml)
    raw["routes"]["bridge"]["context_map"]["room"].update(extra)
    with pytest.raises(ConfigValidationError, match="not a simple guided"):
        draft_bridge(
            dump_yaml_config(raw),
            BridgeRequest("left", "right", (("room", "other"),), replace_route=True),
            paths=resolve(),
        )


def test_explain_reports_known_denial_with_unspecified_sender(provider):
    draft = draft_bridge(
        CONFIG, BridgeRequest("left", "right", (("room", "topic"),)), paths=resolve()
    )
    raw = parse_yaml_config(draft.yaml)
    raw["routes"]["bridge"]["policy"].update(
        sender_allowlist=["approved"], channel_allowlist=["different"]
    )
    result = explain_routes(
        parse_runtime_config(raw, resolve()),
        source_adapter="left",
        source_context="room",
        event_kind="message.text",
    )
    target = result.matches[0].targets[0]
    assert target["policy"] == "policy_denied"
    assert target["reason"] == "channel_not_allowed"
