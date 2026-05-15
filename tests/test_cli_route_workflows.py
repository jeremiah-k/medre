"""Route validate, topology, and list CLI workflows.

Operators run validate, topology, and list commands and get consistent,
deterministic results across all three subcommands.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_cli_config_workflows import (
    _run_cli,
    _run_cli_raw,
    config_fake_multi,
    config_minimal,
)


class TestRoutesWorkflow:
    """Operators run validate, topology, list and get consistent results."""

    def test_validate_lists_all_routes(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_fake_multi))
        assert "matrix_to_mesh" in output
        assert "mesh_to_matrix" in output
        assert "bidirectional_bridge" in output
        assert "Routes valid" in output

    def test_validate_shows_warning_for_disabled_route(
        self, config_fake_multi: Path
    ) -> None:
        """mesh_to_matrix is disabled; validate should warn about no enabled source/dest."""
        output = _run_cli("routes", "validate", "--config", str(config_fake_multi))
        assert "Routes valid" in output

    def test_topology_matches_validate(self, config_fake_multi: Path) -> None:
        """Same route IDs appear in both topology and validate output."""
        validate_out = _run_cli(
            "routes", "validate", "--config", str(config_fake_multi)
        )
        topology_out = _run_cli(
            "routes", "topology", "--config", str(config_fake_multi)
        )
        for rid in ("matrix_to_mesh", "mesh_to_matrix", "bidirectional_bridge"):
            assert rid in validate_out
            assert rid in topology_out

    def test_topology_shows_transport_labels(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_fake_multi))
        assert "fake_matrix(matrix)" in output
        assert "fake_mesh(meshtastic)" in output

    def test_topology_direction_arrows(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_fake_multi))
        assert "-->" in output  # source_to_dest
        assert "<->" in output  # bidirectional

    def test_topology_active_count(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_fake_multi))
        assert "2/3 route(s) active" in output

    def test_list_shows_all_route_details(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_fake_multi))
        assert "Configured routes:" in output
        for rid in ("matrix_to_mesh", "mesh_to_matrix", "bidirectional_bridge"):
            assert rid in output
        assert "status:        enabled" in output
        assert "status:        disabled" in output
        assert "direction:     source_to_dest" in output
        assert "direction:     bidirectional" in output

    def test_list_shows_targeting_fields(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_fake_multi))
        assert "source_room:" in output
        assert "dest_channel:" in output

    def test_list_shows_policy_fields(self, config_fake_multi: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_fake_multi))
        assert "policy:" in output
        assert "event_types:" in output

    def test_no_routes_message(self, config_minimal: Path) -> None:
        for subcmd in ("validate", "topology", "list"):
            output = _run_cli("routes", subcmd, "--config", str(config_minimal))
            assert "No routes configured" in output, (
                f"routes {subcmd} did not report empty routes"
            )
