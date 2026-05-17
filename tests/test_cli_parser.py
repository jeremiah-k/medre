"""Tests for CLI argument parsing and dispatch."""

from __future__ import annotations

import pytest

from medre.cli import main


class TestCLIParser:
    """Tests for CLI argument parsing and dispatch."""

    def test_routes_validate_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes"])

    def test_routes_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "nonexistent"])

    def test_routes_validate_has_config_flag(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "validate", "--config", "/nonexistent/path.toml"])

    def test_routes_topology_has_config_flag(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "topology", "--config", "/nonexistent/path.toml"])

    def test_routes_list_has_config_flag(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "list", "--config", "/nonexistent/path.toml"])


def test_matrix_not_toplevel() -> None:
    """'matrix' must not be accepted as a top-level command."""
    with pytest.raises(SystemExit):
        main(["matrix"])


def test_meshtastic_not_toplevel() -> None:
    """'meshtastic' must not be accepted as a top-level command."""
    with pytest.raises(SystemExit):
        main(["meshtastic"])


def test_lxmf_not_toplevel() -> None:
    """'lxmf' must not be accepted as a top-level command."""
    with pytest.raises(SystemExit):
        main(["lxmf"])


def test_auth_not_toplevel() -> None:
    """'auth' must not be accepted as a top-level command (moved to adapter namespace)."""
    with pytest.raises(SystemExit):
        main(["auth"])


def test_adapter_abbrev_rejected_in_auth_login() -> None:
    """--adapter must not be accepted as abbreviation for --adapter-id in auth login."""
    from medre.cli.main import _build_parser
    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--config", "/tmp/x.toml",
            "--adapter", "m",
            "--homeserver", "https://x.org",
            "--user", "@x:x.org",
        ])
