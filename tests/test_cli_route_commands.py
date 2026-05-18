"""Tests for 'medre routes validate/topology/list' commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.cli import (
    CONFIG_DISABLED_ADAPTER_IN_ROUTE,
    CONFIG_DISABLED_ROUTE_UNKNOWN_REFS,
    CONFIG_MINIMAL,
    CONFIG_NO_ROUTES,
    CONFIG_ROUTE_UNKNOWN_ADAPTERS,
    CONFIG_WITH_ROUTE_TARGETING,
    CONFIG_WITH_ROUTES,
    _run_cli,
    _run_cli_both,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def config_with_routes(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_WITH_ROUTES)
    return p


@pytest.fixture()
def config_no_routes(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_NO_ROUTES)
    return p


@pytest.fixture()
def config_with_targeting(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_WITH_ROUTE_TARGETING)
    return p


@pytest.fixture()
def config_unknown_adapters(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_ROUTE_UNKNOWN_ADAPTERS)
    return p


@pytest.fixture()
def config_minimal(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_MINIMAL)
    return p


@pytest.fixture()
def config_disabled_adapter_in_route(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_DISABLED_ADAPTER_IN_ROUTE)
    return p


@pytest.fixture()
def config_disabled_route_unknown_refs(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_DISABLED_ROUTE_UNKNOWN_REFS)
    return p


# ---------------------------------------------------------------------------
# routes validate
# ---------------------------------------------------------------------------


class TestRoutesValidate:
    """Tests for 'medre routes validate' command."""

    def test_validate_with_routes(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_with_routes))
        assert "matrix_to_radio: enabled" in output
        assert "radio_to_matrix: disabled" in output
        assert "bidirectional_bridge: enabled" in output
        assert "Routes valid" in output

    def test_validate_shows_on_off_markers(self, config_with_routes: Path) -> None:
        """Validate output includes [ON]/[OFF] per route."""
        output = _run_cli("routes", "validate", "--config", str(config_with_routes))
        assert "[ON]" in output
        assert "[OFF]" in output

    def test_validate_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_no_routes))
        assert "No routes configured" in output

    def test_validate_shows_direction(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_with_routes))
        assert "source_to_dest" in output
        assert "bidirectional" in output

    def test_validate_unknown_adapter_errors(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown adapter IDs in enabled routes are errors, not warnings."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("routes", "validate", "--config", str(config_unknown_adapters))
        assert exc_info.value.code == EXIT_CONFIG
        # Capture output from the SystemExit path via _run_cli_both
        stdout, _stderr = _run_cli_both(
            "routes", "validate", "--config", str(config_unknown_adapters)
        )
        assert "nonexistent" in stdout
        assert "also_missing" in stdout
        assert "orphan_route" in stdout
        assert "\u2717" in stdout  # ✗ error marker

    def test_validate_unknown_adapter_names_specific_id(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown adapter errors name the specific adapter ID."""
        stdout, _stderr = _run_cli_both(
            "routes", "validate", "--config", str(config_unknown_adapters)
        )
        assert "source adapter" in stdout or "source" in stdout
        assert "'nonexistent'" in stdout
        assert "dest adapter" in stdout or "dest" in stdout
        assert "'also_missing'" in stdout

    def test_validate_shows_known_adapter_ids(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown adapter errors mention the known adapter IDs for guidance."""
        stdout, _stderr = _run_cli_both(
            "routes", "validate", "--config", str(config_unknown_adapters)
        )
        assert "Known adapter IDs" in stdout
        assert "main" in stdout

    def test_validate_minimal_config(self, config_minimal: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_minimal))
        assert "No routes configured" in output

    def test_validate_groups_errors_by_route(
        self, config_unknown_adapters: Path
    ) -> None:
        """Errors are shown grouped under their route, not flat-listed."""
        stdout, _stderr = _run_cli_both(
            "routes", "validate", "--config", str(config_unknown_adapters)
        )
        lines = stdout.splitlines()
        orphan_line_idx = None
        for i, line in enumerate(lines):
            if "orphan_route" in line:
                orphan_line_idx = i
                break
        assert orphan_line_idx is not None
        following = "\n".join(lines[orphan_line_idx:])
        assert "nonexistent" in following
        assert "also_missing" in following

    def test_validate_missing_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _run_cli(
                "routes", "validate", "--config", str(tmp_path / "nonexistent.toml")
            )

    def test_validate_unknown_source_exits_config(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown source adapter in enabled route exits EXIT_CONFIG=2."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "routes",
                    "validate",
                    "--config",
                    str(config_unknown_adapters),
                ]
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_validate_unknown_dest_exits_config(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown dest adapter in enabled route exits EXIT_CONFIG=2."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "routes",
                    "validate",
                    "--config",
                    str(config_unknown_adapters),
                ]
            )
        assert exc_info.value.code == EXIT_CONFIG
        stdout, _ = _run_cli_both(
            "routes",
            "validate",
            "--config",
            str(config_unknown_adapters),
        )
        assert "'also_missing'" in stdout

    def test_validate_known_disabled_adapter_is_warning(
        self, config_disabled_adapter_in_route: Path
    ) -> None:
        """Route referencing a known-but-disabled adapter is a warning, not an error."""
        output = _run_cli(
            "routes",
            "validate",
            "--config",
            str(config_disabled_adapter_in_route),
        )
        assert "warning" in output.lower() or "\u26a0" in output
        assert "disabled" in output.lower()

    def test_validate_disabled_route_with_unknown_refs_passes(
        self, config_disabled_route_unknown_refs: Path
    ) -> None:
        """Unknown adapter refs in a disabled route do not fail validation."""
        output = _run_cli(
            "routes",
            "validate",
            "--config",
            str(config_disabled_route_unknown_refs),
        )
        assert "ghost_route" in output
        assert "[OFF]" in output
        assert "Routes valid" in output

    def test_validate_valid_config_exits_cleanly(
        self, config_with_routes: Path
    ) -> None:
        """Valid route configuration exits 0."""
        output = _run_cli(
            "routes",
            "validate",
            "--config",
            str(config_with_routes),
        )
        assert "Routes valid" in output


# ---------------------------------------------------------------------------
# routes topology
# ---------------------------------------------------------------------------


class TestRoutesTopology:
    """Tests for 'medre routes topology' command."""

    def test_topology_with_routes(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "Route topology:" in output
        assert "matrix_to_radio" in output
        assert "radio_to_matrix" in output
        assert "bidirectional_bridge" in output

    def test_topology_shows_transport_labels(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "main(matrix)" in output
        assert "radio(meshtastic)" in output

    def test_topology_direction_arrows(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "-->" in output  # source_to_dest
        assert "<->" in output  # bidirectional

    def test_topology_disabled_route(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "[OFF]" in output
        assert "disabled" not in output or "radio_to_matrix" in output

    def test_topology_enabled_disabled_markers(self, config_with_routes: Path) -> None:
        """Topology uses [ON] and [OFF] prefixes for routes."""
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "[ON]" in output
        assert "[OFF]" in output

    def test_topology_targeting_fields(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "src_room=" in output
        assert "dst_ch=" in output

    def test_topology_policy_shown(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "policy:" in output
        assert "events=message" in output

    def test_topology_no_filter_hooks_shown(self, config_with_routes: Path) -> None:
        """filter_hooks are rejected at parse time, so they never appear in output."""
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "hooks:" not in output

    def test_topology_summary(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "2/3 route(s) active" in output

    def test_topology_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_no_routes))
        assert "No routes configured" in output

    def test_topology_full_targeting(self, config_with_targeting: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_targeting))
        assert "src_room=" in output
        assert "dst_room=" in output

    def test_topology_full_policy(self, config_with_targeting: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_targeting))
        assert "events=message,reaction" in output


# ---------------------------------------------------------------------------
# routes list
# ---------------------------------------------------------------------------


class TestRoutesList:
    """Tests for 'medre routes list' command."""

    def test_list_with_routes(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "Configured routes:" in output
        assert "matrix_to_radio:" in output
        assert "radio_to_matrix:" in output
        assert "bidirectional_bridge:" in output

    def test_list_shows_status(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "status:        enabled" in output
        assert "status:        disabled" in output

    def test_list_shows_direction(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "direction:     source_to_dest" in output
        assert "direction:     bidirectional" in output

    def test_list_shows_sources_and_dests(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "sources:       [main]" in output
        assert "destinations:  [radio]" in output
        assert "sources:       [radio]" in output
        assert "destinations:  [main]" in output

    def test_list_shows_targeting(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "source_room:" in output
        assert "dest_channel:" in output

    def test_list_shows_policy(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "policy:" in output
        assert "event_types:" in output

    def test_list_no_filter_hooks_shown(self, config_with_routes: Path) -> None:
        """filter_hooks are rejected at parse time, so they never appear in output."""
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "filter_hooks:" not in output

    def test_list_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_no_routes))
        assert "No routes configured" in output

    def test_list_full_targeting_and_policy(self, config_with_targeting: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_targeting))
        assert "source_room:" in output
        assert "dest_room:" in output
        assert "event_types:" in output
