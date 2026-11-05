"""Typed, informational setup journeys for form-less providers.

A provider that cannot be an offline form may declare
``AdapterSpec.native_setup_instructions``. The declaration is optional with
the same absence semantics as ``native_setup_fields``: without it, discovery
and every existing refusal are unchanged. Rendering through
``medre config native`` is offline and informational only: it never invokes
a provider command, never writes a file, and never claims provisioning
occurred or grants send authority.
"""

from __future__ import annotations

import io
from argparse import Namespace
from dataclasses import replace
from hashlib import sha256
from unittest.mock import patch

import pytest

from medre import adapter_registry
from medre.adapter_spec import (
    AdapterSpec,
    NativeSetupField,
    NativeSetupInstructions,
    NativeSetupStep,
    SymbolRef,
)
from medre.cli import native_commands
from medre.config.errors import ConfigValidationError
from tests.helpers.bridge_setup import install_provider
from tests.helpers.cli import _run_cli_raw


def _journey() -> NativeSetupInstructions:
    return NativeSetupInstructions(
        title="Provision the provider profile",
        summary=(
            "The provider contributes its own provisioning commands; each "
            "stage states what its observation proves and leaves unproven."
        ),
        steps=(
            NativeSetupStep(
                command="medre adapter independent_chat init --adapter chat",
                purpose="Create the dedicated native profile.",
                evidence_note=(
                    "Proves the native tooling reports a configured profile. "
                    "Does not prove relay service acceptance, message "
                    "delivery, or read evidence, and grants no send authority."
                ),
            ),
            NativeSetupStep(
                command="medre adapter independent_chat chats --adapter chat",
                purpose="List accepted conversations of the profile.",
                evidence_note=(
                    "Proves the native core lists this conversation. Does not "
                    "prove that bridged messages reach their recipients."
                ),
            ),
        ),
    )


def _register(monkeypatch, tmp_path, spec):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        adapter_registry.AdapterTypeRegistry((spec,)),
    )
    return spec


@pytest.fixture
def instructions_provider(monkeypatch, tmp_path):
    spec = replace(install_provider(monkeypatch), native_setup_instructions=_journey())
    return _register(monkeypatch, tmp_path, spec)


def _snapshot_test_files(root):
    """Include nested paths and existing file contents, not just new siblings."""
    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
    }


def _render_to_output(provider, tmp_path, monkeypatch):
    """Run the native command against an instructions-only provider."""
    monkeypatch.setattr("sys.stdin", io.StringIO("must-not-be-read"))
    output = tmp_path / "native.yaml"
    native_commands._config_native(
        Namespace(
            transport=provider.transport,
            adapter_id="bot",
            config=None,
            output=str(output),
        )
    )
    assert not output.exists()
    return output


def test_instructions_render_offline_without_a_terminal(
    instructions_provider, tmp_path, monkeypatch, capsys
):
    _render_to_output(instructions_provider, tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert "Provision the provider profile" in out
    assert (
        "medre adapter independent_chat init --adapter chat" in out
    ), "the verbatim provider command must be rendered unchanged"
    assert "medre adapter independent_chat chats --adapter chat" in out
    assert "Step 1:" in out and "Step 2:" in out
    assert "informational_only" in out
    assert "core never invokes them" in out
    assert "never grants send authority" in out
    assert "No configuration file was written." in out
    assert "provisioned" not in out, "rendering must not claim provisioning occurred"
    assert str(tmp_path) not in out, "rendering must not echo operator paths"


def test_instructions_render_is_deterministic(
    instructions_provider, tmp_path, monkeypatch, capsys
):
    first = _render_to_output(instructions_provider, tmp_path, monkeypatch)
    first_out = capsys.readouterr().out
    second = _render_to_output(instructions_provider, tmp_path, monkeypatch)
    second_out = capsys.readouterr().out
    assert first == second
    assert first_out == second_out


def test_instructions_render_writes_nothing(
    instructions_provider, tmp_path, monkeypatch
):
    before = _snapshot_test_files(tmp_path)
    _render_to_output(instructions_provider, tmp_path, monkeypatch)
    assert _snapshot_test_files(tmp_path) == before


def test_rendered_output_stays_on_stdout(
    instructions_provider, tmp_path, monkeypatch, capsys
):
    _render_to_output(instructions_provider, tmp_path, monkeypatch)
    captured = capsys.readouterr()
    assert "informational_only" in captured.out
    assert "informational_only" not in captured.err


def test_cli_native_renders_journey_and_exits_zero(
    instructions_provider, tmp_path, monkeypatch
):
    _register(monkeypatch, tmp_path, instructions_provider)
    monkeypatch.setattr("sys.stdin", io.StringIO("must-not-be-read"))
    output = tmp_path / "native.yaml"
    stdout, stderr, code = _run_cli_raw(
        "config",
        "native",
        "--transport",
        "independent_chat",
        "--adapter-id",
        "bot",
        "--output",
        str(output),
    )
    assert code == 0, stderr
    assert "informational_only" in stdout
    assert "No configuration file was written." in stdout
    assert not output.exists()


def test_formless_provider_without_instructions_still_refuses(
    tmp_path, monkeypatch, capsys
):
    spec = install_provider(monkeypatch)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "state"))

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal(""))
    output = tmp_path / "native.yaml"
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport=spec.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    assert err.value.code == 2
    captured = capsys.readouterr()
    assert "has no provider-declared native setup prompts" in captured.err
    assert "Provision the provider profile" not in captured.out
    assert not output.exists()


def test_malformed_journey_fails_closed_without_writing(tmp_path, monkeypatch, capsys):
    broken = replace(
        install_provider(monkeypatch),
        native_setup_instructions=NativeSetupInstructions(
            title="Broken", summary="journey", steps=()
        ),
    )
    _register(monkeypatch, tmp_path, broken)
    monkeypatch.setattr("sys.stdin", io.StringIO("must-not-be-read"))
    output = tmp_path / "native.yaml"
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport=broken.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    assert err.value.code == 2
    assert "at least one ordered step" in capsys.readouterr().err
    assert not output.exists()


def test_provider_with_fields_keeps_the_guided_form(tmp_path, monkeypatch, capsys):
    spec = _register(
        monkeypatch,
        tmp_path,
        replace(
            install_provider(monkeypatch),
            native_setup_fields=(NativeSetupField("origin_label", "Origin"),),
            native_setup_instructions=_journey(),
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("must-not-be-read"))
    output = tmp_path / "native.yaml"
    with pytest.raises(SystemExit) as err:
        native_commands._config_native(
            Namespace(
                transport=spec.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    # The guided form stays authoritative: without a terminal it refuses
    # instead of falling back to rendering the declared journey.
    assert err.value.code == 2
    captured = capsys.readouterr()
    assert "requires an interactive terminal" in captured.err
    assert "Provision the provider profile" not in captured.out
    assert not output.exists()


def test_guided_form_flow_is_unchanged_with_instructions_also_declared(
    tmp_path, monkeypatch
):
    spec = _register(
        monkeypatch,
        tmp_path,
        replace(
            install_provider(monkeypatch),
            native_setup_fields=(NativeSetupField("origin_label", "Origin"),),
            native_setup_instructions=_journey(),
        ),
    )

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal("Chat A\n"))
    output = tmp_path / "native.yaml"
    with patch("medre.cli.bridge_commands._confirm_save", return_value=True):
        native_commands._config_native(
            Namespace(
                transport=spec.transport,
                adapter_id="bot",
                config=None,
                output=str(output),
            )
        )
    raw = output.read_text()
    assert "origin_label: Chat A" in raw
    assert "Provision the provider profile" not in raw


def _spec_with(journey) -> AdapterSpec:
    symbol = SymbolRef("builtins", "object")
    return AdapterSpec(
        api_version=1,
        transport="independent_chat",
        config=symbol,
        runtime_config=None,
        adapter=symbol,
        renderer_factory=None,
        dependency_probe=None,
        distribution=None,
        import_names=(),
        sdk_import_roots=(),
        native_namespace_reader=symbol,
        versioned_namespace_reader=symbol,
        attribution_projector=symbol,
        native_setup_instructions=journey,
    )


@pytest.mark.parametrize(
    "journey",
    [
        "not-instructions",
        None,
        NativeSetupInstructions(title=" ", summary="present", steps=_journey().steps),
        NativeSetupInstructions(title="t", summary="", steps=_journey().steps),
        NativeSetupInstructions(title="t", summary="s", steps=()),
        NativeSetupInstructions(title="t", summary="s", steps=("nope",)),
        NativeSetupInstructions(title="t", summary="s", steps=list(_journey().steps)),
        replace(_journey().steps[0], command=""),
        replace(_journey().steps[0], purpose="  "),
        replace(_journey().steps[0], evidence_note=""),
    ],
)
def test_malformed_instructions_are_rejected(journey):
    with pytest.raises(ConfigValidationError):
        native_commands._validate_setup_instructions(_spec_with(journey))


def test_valid_journey_passes_structural_validation(instructions_provider):
    validated = native_commands._validate_setup_instructions(instructions_provider)
    assert validated is instructions_provider.native_setup_instructions
