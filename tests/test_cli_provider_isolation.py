"""A broken optional provider CLI cannot disable unrelated MEDRE commands."""

from dataclasses import replace

import pytest
from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry
from medre.adapter_spec import SymbolRef
from medre.cli.main import _build_parser, main
from tests.helpers.adapter_spec import make_adapter_spec


def register_cli(parser):
    parser.add_subparsers(dest="provider_action", required=True).add_parser("status")


def register_broken_cli(parser):
    parser.add_argument("--partial-contribution")
    raise RuntimeError("registration fault, access_token=synthetic-secret")


def dispatch_cli(args):
    return True


def register_reserved_cli(parser):
    parser.add_subparsers(dest="command").add_parser("run")


def register_reserved_group_cli(parser):
    parser.add_argument_group("Options").add_argument("--command")


def register_reserved_defaults_cli(parser):
    child = parser.add_subparsers(dest="provider_action").add_parser("status")
    child.set_defaults(adapter_command="another_provider")


def register_invalid_formatter_cli(parser):
    parser.formatter_class = None


def _spec(name, register="register_cli", dispatch="dispatch_cli"):
    return replace(
        make_adapter_spec(name),
        cli_register=SymbolRef(__name__, register),
        cli_dispatch=SymbolRef(__name__, dispatch),
    )


@pytest.mark.parametrize(
    "bad_spec",
    [
        replace(
            _spec("broken_chat"),
            cli_register=SymbolRef("unavailable_cli_module", "register"),
        ),
        _spec("broken_chat", register="register_broken_cli"),
        _spec("broken_chat", dispatch="__doc__"),
        _spec("broken_chat", register="register_reserved_cli"),
        _spec("broken_chat", register="register_reserved_group_cli"),
        _spec("broken_chat", register="register_reserved_defaults_cli"),
        _spec("broken_chat", register="register_invalid_formatter_cli"),
    ],
)
def test_broken_contribution_preserves_help_and_reports_preflight_failure(
    bad_spec, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        AdapterTypeRegistry((bad_spec, _spec("healthy_chat"))),
    )
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()
    main(["version"])
    assert "medre" in capsys.readouterr().out
    config = tmp_path / "empty.yaml"
    config.write_text("{}\n")
    with pytest.raises(SystemExit) as exc:
        main(["adapters", "check", "--config", str(config)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "broken_chat: broken CLI contribution" in captured.out
    assert "synthetic-secret" not in captured.out + captured.err
    args = _build_parser().parse_args(["adapter", "healthy_chat", "status"])
    assert args.adapter_command == "healthy_chat"
    assert args.provider_action == "status"


def test_provider_parsers_are_scoped_to_their_transport(monkeypatch):
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        AdapterTypeRegistry((_spec("private_chat"), _spec("other_chat"))),
    )
    parser = _build_parser()
    for transport in ("private_chat", "other_chat"):
        args = parser.parse_args(["adapter", transport, "status"])
        assert args.adapter_command == transport
        assert args.provider_action == "status"


def test_broken_cli_does_not_remove_runtime_provider(monkeypatch):
    spec = _spec("private_chat", register="register_broken_cli")
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    _build_parser()
    assert adapter_registry.get_adapter_spec("private_chat") is spec
