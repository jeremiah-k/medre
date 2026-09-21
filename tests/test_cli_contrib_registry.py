"""Tests for CLI contribution registry and namespace restrictions."""

from __future__ import annotations

import argparse
import sys
import types
from unittest import mock

import pytest

from medre.cli.contrib import (
    ALLOWED_NAMESPACES,
    DISALLOWED_TOPLEVEL,
    dispatch_contribution,
    register_builtin_contributors,
)


def test_register_builtin_contributors_creates_auth_parser() -> None:
    """register_builtin_contributors adds adapter/matrix/auth/login subparser."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_builtin_contributors(subparsers)

    args = parser.parse_args(
        [
            "adapter",
            "matrix",
            "auth",
            "login",
            "--homeserver",
            "https://x.org",
            "--user",
            "@x:x.org",
        ]
    )
    assert args.command == "adapter"
    assert args.adapter_command == "matrix"
    assert args.adapter_matrix_command == "auth"
    assert args.adapter_matrix_auth_command == "login"
    assert args.homeserver == "https://x.org"
    assert args.user == "@x:x.org"


def test_dispatch_contribution_routes_auth_login() -> None:
    """dispatch_contribution calls _adapter_matrix_auth_login for adapter matrix auth login."""
    args = types.SimpleNamespace(
        command="adapter",
        adapter_command="matrix",
        adapter_matrix_command="auth",
        adapter_matrix_auth_command="login",
        config="/tmp/x.toml",
        adapter_id="m",
        homeserver="https://x.org",
        user="@x:x.org",
    )
    async_fn = mock.AsyncMock()
    with mock.patch(
        "medre.adapters.matrix.cli._adapter_matrix_auth_login",
        async_fn,
    ):
        dispatch_contribution(args)
    async_fn.assert_called_once_with(args)


def test_contributors_deterministic_order() -> None:
    """register_builtin_contributors is callable and returns without error."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    result = register_builtin_contributors(subparsers)
    assert result is None


def test_no_sdk_import_during_parser_build() -> None:
    """Building the parser must not import optional SDK packages."""
    # Remove if previously loaded by an unrelated test
    sys.modules.pop("mindroom_nio", None)

    from medre.cli.main import _build_parser

    _build_parser()

    assert "mindroom_nio" not in sys.modules


def test_allowed_namespaces_defined() -> None:
    """adapter is an allowed top-level namespace."""
    assert "adapter" in ALLOWED_NAMESPACES


def test_disallowed_toplevel_defined() -> None:
    """Transport names are disallowed as top-level commands."""
    assert "matrix" in DISALLOWED_TOPLEVEL
    assert "meshtastic" in DISALLOWED_TOPLEVEL
    assert "lxmf" in DISALLOWED_TOPLEVEL


def test_dispatch_contribution_rejects_parser_without_dispatch() -> None:
    """A visible adapter command cannot silently succeed without dispatch."""
    args = types.SimpleNamespace(command="adapter", adapter_command="sample")
    spec = types.SimpleNamespace(cli_dispatch=None)
    with mock.patch("medre.cli.contrib.get_adapter_spec", return_value=spec):
        with pytest.raises(RuntimeError, match="registered a parser.*no dispatch hook"):
            dispatch_contribution(args)


# ---------------------------------------------------------------------------
# Adapter-owned Matrix dispatch hook
# ---------------------------------------------------------------------------


def _matrix_args(**overrides: object) -> types.SimpleNamespace:
    """Parsed-args namespace shaped like ``medre adapter matrix ...``."""
    base: dict[str, object] = {
        "command": "adapter",
        "adapter_command": "matrix",
        "adapter_matrix_command": None,
        "adapter_matrix_auth_command": None,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_dispatch_matrix_cli_ignores_other_transports() -> None:
    from medre.adapters.matrix.cli_contrib import dispatch_matrix_cli

    args = types.SimpleNamespace(adapter_command="meshtastic")
    assert dispatch_matrix_cli(args) is False


def test_dispatch_matrix_cli_routes_auth_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medre.adapters.matrix import cli_contrib

    status_fn = mock.AsyncMock()
    monkeypatch.setattr(
        "medre.adapters.matrix.cli._adapter_matrix_auth_status", status_fn
    )
    args = _matrix_args(
        adapter_matrix_command="auth", adapter_matrix_auth_command="status"
    )
    assert cli_contrib.dispatch_matrix_cli(args) is True
    status_fn.assert_awaited_once_with()


def test_dispatch_matrix_cli_auth_unknown_subcommand_returns_false() -> None:
    from medre.adapters.matrix.cli_contrib import dispatch_matrix_cli

    args = _matrix_args(
        adapter_matrix_command="auth", adapter_matrix_auth_command="bogus"
    )
    assert dispatch_matrix_cli(args) is False


def test_dispatch_matrix_cli_routes_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    from medre.adapters.matrix import cli_contrib

    provision_fn = mock.AsyncMock()
    monkeypatch.setattr(
        "medre.adapters.matrix.cli._adapter_matrix_provision", provision_fn
    )
    args = _matrix_args(adapter_matrix_command="provision")
    assert cli_contrib.dispatch_matrix_cli(args) is True
    provision_fn.assert_awaited_once_with(args)


def test_dispatch_matrix_cli_unknown_command_returns_false() -> None:
    from medre.adapters.matrix.cli_contrib import dispatch_matrix_cli

    assert dispatch_matrix_cli(_matrix_args(adapter_matrix_command="bogus")) is False


# ---------------------------------------------------------------------------
# Generic contribution dispatch guards
# ---------------------------------------------------------------------------


def test_dispatch_contribution_ignores_non_adapter_commands() -> None:
    dispatch_contribution(types.SimpleNamespace(command="smoke"))


def test_dispatch_contribution_ignores_non_string_transport() -> None:
    args = types.SimpleNamespace(command="adapter", adapter_command=None)
    dispatch_contribution(args)


def test_dispatch_contribution_ignores_unregistered_transport() -> None:
    with mock.patch("medre.cli.contrib.get_adapter_spec", return_value=None):
        dispatch_contribution(
            types.SimpleNamespace(command="adapter", adapter_command="sample")
        )


def test_dispatch_contribution_rejects_unhandled_command() -> None:
    spec = types.SimpleNamespace(
        cli_dispatch=types.SimpleNamespace(load=lambda: lambda args: False)
    )
    with mock.patch("medre.cli.contrib.get_adapter_spec", return_value=spec):
        with pytest.raises(RuntimeError, match="did not handle its parsed command"):
            dispatch_contribution(
                types.SimpleNamespace(command="adapter", adapter_command="sample")
            )


def test_register_builtin_contributors_without_contributors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registered CLI contributors means no adapter namespace at all."""
    monkeypatch.setattr("medre.cli.contrib.iter_adapter_specs", lambda: ())
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_builtin_contributors(subparsers)
    assert "adapter" not in subparsers.choices
