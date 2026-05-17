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
