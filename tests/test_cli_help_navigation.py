"""Help navigation follows registered command paths without executing handlers."""

from dataclasses import replace

import pytest

from tests.helpers.adapter_spec import make_adapter_spec
from tests.helpers.cli import _run_cli_raw


def test_help_navigates_without_adapter_imports() -> None:
    for words, expected in (
        (("help",), "Modular Event-driven Routing Engine"),
        (("help", "routes"), "diagram"),
        (("help", "routes", "diagram"), "--full-context"),
        (("--help", "routes", "diagram"), "--full-context"),
        (("routes",), "validate"),
    ):
        out, err, code = _run_cli_raw(*words)
        assert code == 0, err
        assert expected in out


def test_bare_nested_groups_are_helpful_without_running_a_handler():
    groups = ["config", "support", "dev", "trace"]
    for group in groups:
        output, error, code = _run_cli_raw(group)
        assert code == 0, error
        assert "usage:" in output
    _, error, code = _run_cli_raw("config", "typo")
    assert code == 2 and "invalid choice" in error


def register_help_contributor(parser):
    """Declare a controlled provider command without loading a transport SDK."""
    parser.add_subparsers(dest="help_action", required=True).add_parser("status")


def dispatch_help_contributor(_args):
    """Reject dispatch because help must never invoke a provider command."""
    pytest.fail("Help invoked a provider command")


def test_help_traverses_a_valid_provider_contribution(monkeypatch):
    from medre import adapter_registry
    from medre.adapter_registry import AdapterTypeRegistry
    from medre.adapter_spec import SymbolRef

    spec = replace(
        make_adapter_spec("controlled"),
        cli_register=SymbolRef(__name__, "register_help_contributor"),
        cli_dispatch=SymbolRef(__name__, "dispatch_help_contributor"),
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    for words in (("adapter",), ("help", "adapter", "controlled", "status")):
        output, error, code = _run_cli_raw(*words)
        assert code == 0, error
        assert "controlled" in output


def test_config_option_help_explains_environment_precedence():
    output, error, code = _run_cli_raw("help", "run")
    assert code == 0, error
    assert output.index("MEDRE_CONFIG") < output.index("saved selection")
    assert "$MEDRE_HOME/config.yaml" in output


@pytest.mark.parametrize("prefix", ["help", "--help"])
def test_explicit_help_cannot_be_delegated_to_a_subprocess(monkeypatch, prefix):
    def refuse(_args):
        pytest.fail("Explicit MEDRE help invoked a development command")

    monkeypatch.setattr("medre.cli.dev_commands._development", refuse)
    output, error, code = _run_cli_raw(prefix, "dev", "test", "matrix", "--", "-x")
    assert code == 0, error
    assert "usage: medre dev test" in output and "pytest_args" in output


def test_explicit_help_rejects_unknown_commands_without_dispatch(monkeypatch):
    def refuse(_args):
        pytest.fail("Invalid help path invoked a development command")

    monkeypatch.setattr("medre.cli.dev_commands._development", refuse)
    _, error, code = _run_cli_raw("help", "dev", "unknown-command")
    assert code == 2 and "invalid choice" in error


def test_pytest_help_is_forwarded_only_for_a_normal_development_command(monkeypatch):
    received = []
    monkeypatch.setattr("medre.cli.dev_commands._development", received.append)
    _, error, code = _run_cli_raw("dev", "test", "matrix", "--", "--help")
    assert code == 0, error
    assert len(received) == 1
    assert received[0].adapter == "matrix" and "--help" in received[0].pytest_args
