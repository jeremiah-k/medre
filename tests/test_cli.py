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
filter_hooks = ["spam_filter"]

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
sender_allowlist = ["@alice:test"]
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
source_channel = "ch-src"
dest_channel = "ch-dst"

[routes.targeted_route.policy]
allowed_event_types = ["message", "reaction"]
room_allowlist = ["!room:test", "!room2:test"]
channel_allowlist = ["ch-src", "ch-dst"]
sender_allowlist = ["@alice:test", "@bob:test"]
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
        assert "Warning" in output
        assert "nonexistent" in output
        assert "also_missing" in output

    def test_validate_minimal_config(self, config_minimal: Path) -> None:
        output = _run_cli("routes", "validate", "--config", str(config_minimal))
        assert "No routes configured" in output

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
        # radio_to_matrix is disabled
        assert "disabled" in output

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
        assert "senders=" in output

    def test_topology_filter_hooks(self, config_with_routes: Path) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "hooks:" in output
        assert "spam_filter" in output

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
        assert "src_ch=" in output
        assert "dst_ch=" in output

    def test_topology_full_policy(
        self, config_with_targeting: Path
    ) -> None:
        output = _run_cli("routes", "topology", "--config", str(config_with_targeting))
        assert "events=message,reaction" in output
        assert "rooms=" in output
        assert "channels=" in output
        assert "senders=" in output


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
        assert "senders:" in output

    def test_list_shows_filter_hooks(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "filter_hooks:" in output
        assert "spam_filter" in output

    def test_list_no_routes(self, config_no_routes: Path) -> None:
        output = _run_cli("routes", "list", "--config", str(config_no_routes))
        assert "No routes configured" in output

    def test_list_full_targeting_and_policy(
        self, config_with_targeting: Path
    ) -> None:
        output = _run_cli("routes", "list", "--config", str(config_with_targeting))
        assert "source_room:" in output
        assert "dest_room:" in output
        assert "source_channel:" in output
        assert "dest_channel:" in output
        # Policy fields
        assert "event_types:" in output
        assert "rooms:" in output
        assert "channels:" in output
        assert "senders:" in output


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

    def test_config_check_route_summary_count(
        self, config_with_routes: Path
    ) -> None:
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "2/3 route(s) active" in output

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
