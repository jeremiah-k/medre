"""Tests for medre.cli: command dispatch, config check, routes validate/topology/list."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from medre.cli import main


# ---------------------------------------------------------------------------
# Sample TOML configs
# ---------------------------------------------------------------------------

CONFIG_WITH_ROUTES = """\
[runtime]
name = "test-routes"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"

[routes.matrix_to_radio]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true

[routes.radio_to_matrix]
source_adapters = ["radio"]
dest_adapters = ["main"]
directionality = "source_to_dest"
enabled = false

[routes.bidirectional_bridge]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "bidirectional"
enabled = true
source_room = "!room:test"
dest_channel = "1"

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
"""

CONFIG_NO_ROUTES = """\
[runtime]
name = "test-no-routes"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"
"""

CONFIG_WITH_ROUTE_TARGETING = """\
[runtime]
name = "test-targets"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.src]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.matrix.dst]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot2:test"
access_token = "tok2"
room_allowlist = ["!room2:test"]
encryption_mode = "plaintext"

[routes.targeted_route]
source_adapters = ["src"]
dest_adapters = ["dst"]
directionality = "bidirectional"
enabled = true
source_room = "!room:test"
dest_room = "!room2:test"

[routes.targeted_route.policy]
allowed_event_types = ["message", "reaction"]
"""

CONFIG_ROUTE_UNKNOWN_ADAPTERS = """\
[runtime]
name = "test-unknown"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.main]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[routes.orphan_route]
source_adapters = ["nonexistent"]
dest_adapters = ["also_missing"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_MINIMAL = """\
[runtime]
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear config-related env vars for each test."""
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def config_with_routes(tmp_path: Path) -> Path:
    """Write CONFIG_WITH_ROUTES to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_WITH_ROUTES)
    return p


@pytest.fixture()
def config_no_routes(tmp_path: Path) -> Path:
    """Write CONFIG_NO_ROUTES to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_NO_ROUTES)
    return p


@pytest.fixture()
def config_with_targeting(tmp_path: Path) -> Path:
    """Write CONFIG_WITH_ROUTE_TARGETING to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_WITH_ROUTE_TARGETING)
    return p


@pytest.fixture()
def config_unknown_adapters(tmp_path: Path) -> Path:
    """Write CONFIG_ROUTE_UNKNOWN_ADAPTERS to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_ROUTE_UNKNOWN_ADAPTERS)
    return p


@pytest.fixture()
def config_minimal(tmp_path: Path) -> Path:
    """Write CONFIG_MINIMAL to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_MINIMAL)
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str, tmp_path: Path | None = None) -> str:
    """Run CLI with given args, capture stdout, and return output."""
    import io
    from contextlib import redirect_stdout, redirect_stderr

    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        # SystemExit(0) is fine (e.g. --help); non-zero is an error
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


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

    def test_validate_unknown_adapter_warnings(
        self, config_unknown_adapters: Path
    ) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_unknown_adapters))
        assert "Warning" in output or "\u26a0" in output
        assert "nonexistent" in output
        assert "also_missing" in output
        # Improved: should mention the route and section path
        assert "routes.orphan_route" in output or "orphan_route" in output

    def test_validate_unknown_adapter_names_specific_id(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown adapter warnings name the specific adapter ID."""
        output = _run_cli("routes", "validate", "--config", str(config_unknown_adapters))
        # Should mention 'nonexistent' as a source adapter problem
        assert "source adapter" in output or "source" in output
        assert "'nonexistent'" in output
        # Should mention 'also_missing' as a dest adapter problem
        assert "dest adapter" in output or "dest" in output
        assert "'also_missing'" in output

    def test_validate_shows_known_adapter_ids(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown adapter warnings mention the known adapter IDs for guidance."""
        output = _run_cli("routes", "validate", "--config", str(config_unknown_adapters))
        assert "Known adapter IDs" in output
        assert "main" in output

    def test_validate_minimal_config(self, config_minimal: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_minimal))
        assert "No routes configured" in output

    def test_validate_groups_warnings_by_route(
        self, config_unknown_adapters: Path
    ) -> None:
        """Warnings are shown grouped under their route, not flat-listed."""
        output = _run_cli("routes", "validate", "--config", str(config_unknown_adapters))
        lines = output.splitlines()
        # Find the orphan_route line
        orphan_line_idx = None
        for i, line in enumerate(lines):
            if "orphan_route" in line:
                orphan_line_idx = i
                break
        assert orphan_line_idx is not None
        # The next lines should contain the warnings for this route
        following = "\n".join(lines[orphan_line_idx:])
        assert "nonexistent" in following
        assert "also_missing" in following

    def test_validate_missing_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _run_cli("routes", "validate", "--config", str(tmp_path / "nonexistent.toml"))


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

    def test_topology_shows_transport_labels(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        # main is a matrix adapter, radio is meshtastic
        assert "main(matrix)" in output
        assert "radio(meshtastic)" in output

    def test_topology_direction_arrows(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "-->" in output  # source_to_dest
        assert "<->" in output  # bidirectional

    def test_topology_disabled_route(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        # radio_to_matrix is disabled — should show [OFF] marker
        assert "[OFF]" in output
        assert "disabled" not in output or "radio_to_matrix" in output

    def test_topology_enabled_disabled_markers(
        self, config_with_routes: Path
    ) -> None:
        """Topology uses [ON] and [OFF] prefixes for routes."""
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "[ON]" in output
        assert "[OFF]" in output

    def test_topology_targeting_fields(
        self, config_with_routes: Path
    ) -> None:
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

    def test_topology_full_targeting(
        self, config_with_targeting: Path
    ) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_targeting))
        assert "src_room=" in output
        assert "dst_room=" in output

    def test_topology_full_policy(
        self, config_with_targeting: Path
    ) -> None:
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

    def test_list_shows_sources_and_dests(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "sources:       [main]" in output
        assert "destinations:  [radio]" in output
        assert "sources:       [radio]" in output
        assert "destinations:  [main]" in output

    def test_list_shows_targeting(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "source_room:" in output
        assert "dest_channel:" in output

    def test_list_shows_policy(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "policy:" in output
        assert "event_types:" in output

    def test_list_no_filter_hooks_shown(
        self, config_with_routes: Path
    ) -> None:
        """filter_hooks are rejected at parse time, so they never appear in output."""
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "filter_hooks:" not in output

    def test_list_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_no_routes))
        assert "No routes configured" in output

    def test_list_full_targeting_and_policy(
        self, config_with_targeting: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_targeting))
        assert "source_room:" in output
        assert "dest_room:" in output
        # Policy fields
        assert "event_types:" in output


# ---------------------------------------------------------------------------
# config check — route inventory integration
# ---------------------------------------------------------------------------


class TestConfigCheckRoutes:
    """Tests that 'medre config check' includes route inventory."""

    def test_config_check_with_routes(self, config_with_routes: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "Route inventory:" in output
        assert "matrix_to_radio: enabled" in output
        assert "radio_to_matrix: disabled" in output
        assert "Config valid" in output

    def test_config_check_route_on_off_markers(
        self, config_with_routes: Path
    ) -> None:
        """Config check route inventory shows [ON]/[OFF] markers."""
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "[ON]" in output
        assert "[OFF]" in output

    def test_config_check_route_summary_count(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "2/3 route(s) active" in output

    def test_config_check_route_enabled_disabled_summary(
        self, config_with_routes: Path
    ) -> None:
        """Config check includes N route(s) configured (M enabled, K disabled)."""
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "3 route(s) configured (2 enabled, 1 disabled)" in output

    def test_config_check_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_no_routes))
        assert "Route inventory:" in output
        assert "(no routes configured)" in output

    def test_config_check_minimal(self, config_minimal: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_minimal))
        assert "Config valid" in output
        assert "Route inventory:" in output


# ---------------------------------------------------------------------------
# Parser / dispatch tests
# ---------------------------------------------------------------------------


class TestCLIParser:
    """Tests for CLI argument parsing and dispatch."""

    def test_routes_validate_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes"])

    def test_routes_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "nonexistent"])

    def test_routes_validate_has_config_flag(self) -> None:
        # Should fail finding config, but not fail parsing
        with pytest.raises(SystemExit):
            main(["routes", "validate", "--config", "/nonexistent/path.toml"])

    def test_routes_topology_has_config_flag(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "topology", "--config", "/nonexistent/path.toml"])

    def test_routes_list_has_config_flag(self) -> None:
        with pytest.raises(SystemExit):
            main(["routes", "list", "--config", "/nonexistent/path.toml"])


# ---------------------------------------------------------------------------
# Sample config includes route section
# ---------------------------------------------------------------------------


class TestSampleConfig:
    """Tests for 'medre config sample' including routes section."""

    def test_sample_includes_routes_section(self) -> None:
        output = _run_cli("config", "sample")
        assert "[routes." in output
        assert "source_adapters" in output
        assert "dest_adapters" in output
        assert "directionality" in output

    def test_sample_includes_active_bridge_example(self) -> None:
        """Sample includes a clear Matrix <-> Meshtastic bridge example."""
        output = _run_cli("config", "sample")
        assert "matrix_radio_bridge" in output
        assert "bidirectional" in output

    def test_sample_includes_disabled_route_example(self) -> None:
        """Sample includes a commented-out disabled route example."""
        output = _run_cli("config", "sample")
        assert "enabled = false" in output

    def test_sample_includes_fanout_example(self) -> None:
        """Sample includes a commented-out Matrix hub fan-out example."""
        output = _run_cli("config", "sample")
        assert "fanout" in output

    def test_sample_includes_targeting_example(self) -> None:
        """Sample includes a commented-out route with channel/room targeting."""
        output = _run_cli("config", "sample")
        assert "dest_channel" in output
        assert "source_room" in output

    def test_sample_routes_field_documentation(self) -> None:
        """Sample documents required vs optional route fields."""
        output = _run_cli("config", "sample")
        assert "Required fields" in output or "required" in output.lower()

    def test_sample_no_yaml(self) -> None:
        """Sample must not contain YAML syntax markers."""
        output = _run_cli("config", "sample")
        # The sample uses TOML array syntax, not YAML "- " list markers
        # at the start of lines for route data.
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and "=" not in stripped:
                # YAML list items won't have "=" in them; TOML arrays are
                # inline like ["a", "b"]
                pytest.fail(f"Sample appears to contain YAML-style list: {line!r}")
