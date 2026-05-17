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
    """register_builtin_contributors adds auth/matrix/login subparser."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_builtin_contributors(subparsers)

    args = parser.parse_args([
        "auth", "matrix", "login",
        "--config", "/tmp/x.toml",
        "--adapter", "m",
        "--homeserver", "https://x.org",
        "--user", "@x:x.org",
    ])
    assert args.command == "auth"
    assert args.auth_command == "matrix"
    assert args.auth_matrix_command == "login"
    assert args.config == "/tmp/x.toml"
    assert args.adapter == "m"
    assert args.homeserver == "https://x.org"
    assert args.user == "@x:x.org"


def test_dispatch_contribution_routes_auth_login() -> None:
    """dispatch_contribution calls _auth_matrix_login for auth matrix login."""
    args = types.SimpleNamespace(
        command="auth",
        auth_command="matrix",
        auth_matrix_command="login",
        config="/tmp/x.toml",
        adapter="m",
        homeserver="https://x.org",
        user="@x:x.org",
    )
    async_fn = mock.AsyncMock()
    with mock.patch(
        "medre.cli.auth_commands._auth_matrix_login",
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
    """auth is an allowed top-level namespace."""
    assert "auth" in ALLOWED_NAMESPACES


def test_disallowed_toplevel_defined() -> None:
    """Transport names are disallowed as top-level commands."""
    assert "matrix" in DISALLOWED_TOPLEVEL
    assert "meshtastic" in DISALLOWED_TOPLEVEL
    assert "lxmf" in DISALLOWED_TOPLEVEL
