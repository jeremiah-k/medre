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

    args = parser.parse_args([
        "adapter", "matrix", "auth", "login",
        "--homeserver", "https://x.org",
        "--user", "@x:x.org",
    ])
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
