"""Tests for medre.cli: command dispatch, config check, routes validate/topology/list."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout, redirect_stderr
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

CONFIG_BAD_LIMITS = """\
[runtime]
name = "test-bad-limits"

[runtime.limits]
max_inflight_deliveries = -1

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

CONFIG_NO_ADAPTERS = """\
[runtime]
name = "test-no-adapters"

[storage]
backend = "sqlite"
path = "{state}/test.db"
"""

CONFIG_DISABLED_ADAPTER_IN_ROUTE = """\
[runtime]
name = "test-disabled-adapter-route"

[logging]
level = "INFO"

[storage]
backend = "sqlite"
path = "{state}/test.db"

[adapters.matrix.offline]
enabled = false
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.matrix.active]
enabled = true
homeserver = "https://matrix.test"
user_id = "@bot2:test"
access_token = "tok2"
room_allowlist = ["!room2:test"]
encryption_mode = "plaintext"

[routes.uses_disabled]
source_adapters = ["active"]
dest_adapters = ["offline"]
directionality = "source_to_dest"
enabled = true
"""

CONFIG_DISABLED_ROUTE_UNKNOWN_REFS = """\
[runtime]
name = "test-disabled-unknown"

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

[routes.ghost_route]
source_adapters = ["phantom"]
dest_adapters = ["specter"]
directionality = "source_to_dest"
enabled = false
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


@pytest.fixture()
def config_bad_limits(tmp_path: Path) -> Path:
    """Write CONFIG_BAD_LIMITS to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_BAD_LIMITS)
    return p


@pytest.fixture()
def config_no_adapters(tmp_path: Path) -> Path:
    """Write CONFIG_NO_ADAPTERS to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_NO_ADAPTERS)
    return p


@pytest.fixture()
def config_disabled_adapter_in_route(tmp_path: Path) -> Path:
    """Write CONFIG_DISABLED_ADAPTER_IN_ROUTE to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_DISABLED_ADAPTER_IN_ROUTE)
    return p


@pytest.fixture()
def config_disabled_route_unknown_refs(tmp_path: Path) -> Path:
    """Write CONFIG_DISABLED_ROUTE_UNKNOWN_REFS to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_DISABLED_ROUTE_UNKNOWN_REFS)
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str, tmp_path: Path | None = None) -> str:
    """Run CLI with given args, capture stdout, and return output."""
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


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr) pair."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


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
            _run_cli(
                "routes", "validate", "--config", str(config_unknown_adapters)
            )
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
        from medre.cli import EXIT_CONFIG

        stdout, _stderr = _run_cli_both(
            "routes", "validate", "--config", str(config_unknown_adapters)
        )
        # Should mention 'nonexistent' as a source adapter problem
        assert "source adapter" in stdout or "source" in stdout
        assert "'nonexistent'" in stdout
        # Should mention 'also_missing' as a dest adapter problem
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
        # Find the orphan_route line
        orphan_line_idx = None
        for i, line in enumerate(lines):
            if "orphan_route" in line:
                orphan_line_idx = i
                break
        assert orphan_line_idx is not None
        # The next lines should contain the errors for this route
        following = "\n".join(lines[orphan_line_idx:])
        assert "nonexistent" in following
        assert "also_missing" in following

    def test_validate_missing_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _run_cli("routes", "validate", "--config", str(tmp_path / "nonexistent.toml"))

    def test_validate_unknown_source_exits_config(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown source adapter in enabled route exits EXIT_CONFIG=2."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            main([
                "routes", "validate", "--config",
                str(config_unknown_adapters),
            ])
        assert exc_info.value.code == EXIT_CONFIG

    def test_validate_unknown_dest_exits_config(
        self, config_unknown_adapters: Path
    ) -> None:
        """Unknown dest adapter in enabled route exits EXIT_CONFIG=2."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            main([
                "routes", "validate", "--config",
                str(config_unknown_adapters),
            ])
        assert exc_info.value.code == EXIT_CONFIG
        stdout, _ = _run_cli_both(
            "routes", "validate", "--config",
            str(config_unknown_adapters),
        )
        assert "'also_missing'" in stdout

    def test_validate_known_disabled_adapter_is_warning(
        self, config_disabled_adapter_in_route: Path
    ) -> None:
        """Route referencing a known-but-disabled adapter is a warning, not an error."""
        output = _run_cli(
            "routes", "validate", "--config",
            str(config_disabled_adapter_in_route),
        )
        # Should NOT exit EXIT_CONFIG — known disabled is a warning
        assert "warning" in output.lower() or "\u26a0" in output
        assert "disabled" in output.lower()

    def test_validate_disabled_route_with_unknown_refs_passes(
        self, config_disabled_route_unknown_refs: Path
    ) -> None:
        """Unknown adapter refs in a disabled route do not fail validation."""
        output = _run_cli(
            "routes", "validate", "--config",
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
            "routes", "validate", "--config", str(config_with_routes),
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


# ---------------------------------------------------------------------------
# medre version
# ---------------------------------------------------------------------------


class TestVersion:
    """Tests for 'medre version' command."""

    def test_version_output_contains_medre(self) -> None:
        output = _run_cli("version")
        assert "medre" in output

    def test_version_shows_python(self) -> None:
        output = _run_cli("version")
        assert "Python" in output

    def test_version_shows_platform(self) -> None:
        output = _run_cli("version")
        assert "Platform" in output

    def test_version_format(self) -> None:
        """Version output has expected format: medre X.Y.Z"""
        output = _run_cli("version")
        lines = output.strip().splitlines()
        assert lines[0].startswith("medre ")


# ---------------------------------------------------------------------------
# medre paths
# ---------------------------------------------------------------------------


class TestPaths:
    """Tests for 'medre paths' command."""

    def test_paths_shows_config_file(self) -> None:
        output = _run_cli("paths")
        assert "Config file:" in output

    def test_paths_shows_state_dir(self) -> None:
        output = _run_cli("paths")
        assert "State dir:" in output

    def test_paths_shows_data_dir(self) -> None:
        output = _run_cli("paths")
        assert "Data dir:" in output

    def test_paths_shows_log_dir(self) -> None:
        output = _run_cli("paths")
        assert "Log dir:" in output

    def test_paths_shows_global_db(self) -> None:
        output = _run_cli("paths")
        assert "Global DB:" in output


# ---------------------------------------------------------------------------
# config check — error / nonzero exit tests
# ---------------------------------------------------------------------------


class TestConfigCheckErrors:
    """Tests that 'medre config check' exits nonzero on invalid config."""

    def test_missing_config_file(self, tmp_path: Path) -> None:
        """Missing config file causes nonzero exit with clear error message."""
        with pytest.raises(SystemExit) as exc_info:
            _run_cli("config", "check", "--config", str(tmp_path / "missing.toml"))
        assert exc_info.value.code != 0

    def test_missing_config_file_clear_message(self, tmp_path: Path) -> None:
        """Error message is human-readable, not a traceback."""
        _, stderr = _run_cli_both(
            "config", "check", "--config", str(tmp_path / "missing.toml")
        )
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_bad_limits_exits_nonzero(self, config_bad_limits: Path) -> None:
        """Config with invalid limits exits nonzero after validation."""
        with pytest.raises(SystemExit) as exc_info:
            _run_cli("config", "check", "--config", str(config_bad_limits))
        assert exc_info.value.code != 0

    def test_bad_limits_shows_error(self, config_bad_limits: Path) -> None:
        """Bad limits config shows a clear validation error in output."""
        output, stderr = _run_cli_both(
            "config", "check", "--config", str(config_bad_limits)
        )
        combined = (output + stderr).lower()
        assert "error" in combined

    def test_valid_config_exits_zero(self, config_with_routes: Path) -> None:
        """Valid config exits zero (does NOT raise SystemExit)."""
        # _run_cli suppresses SystemExit(0), so no exception means exit 0.
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "Config valid" in output


# ---------------------------------------------------------------------------
# medre diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """Tests for 'medre diagnostics' command."""

    def test_diagnostics_produces_json(self, config_with_routes: Path) -> None:
        """Diagnostics with valid config produces parseable JSON output."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_diagnostics_json_has_adapters_key(
        self, config_with_routes: Path
    ) -> None:
        """Diagnostics JSON contains adapter information."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        # Should contain some structured data about the runtime.
        assert len(parsed) > 0

    def test_diagnostics_missing_config(self, tmp_path: Path) -> None:
        """Missing config file exits nonzero with clear error."""
        _, stderr = _run_cli_both(
            "diagnostics", "--config", str(tmp_path / "missing.toml")
        )
        assert "Config error:" in stderr
        assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# medre run — error handling
# ---------------------------------------------------------------------------


class TestRunErrors:
    """Tests that 'medre run' exits cleanly (no traceback) on config errors."""

    def test_run_missing_config_no_traceback(self, tmp_path: Path) -> None:
        """Missing config causes clear error, not raw traceback."""
        _, stderr = _run_cli_both(
            "run", "--config", str(tmp_path / "missing.toml")
        )
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_run_no_adapters_exits_nonzero(
        self, config_no_adapters: Path
    ) -> None:
        """Run with no enabled adapters exits nonzero with clear message."""
        _, stderr = _run_cli_both(
            "run", "--config", str(config_no_adapters)
        )
        assert "no adapters enabled" in stderr.lower() or "error" in stderr.lower()

    def test_run_no_adapters_clear_message(
        self, config_no_adapters: Path
    ) -> None:
        """Error message mentions adapters, not a traceback."""
        _, stderr = _run_cli_both(
            "run", "--config", str(config_no_adapters)
        )
        assert "Traceback" not in stderr
        assert "adapter" in stderr.lower()


# ---------------------------------------------------------------------------
# medre run — differentiated exit codes
# ---------------------------------------------------------------------------


class TestRunExitCodes:
    """Tests that 'medre run' uses differentiated exit codes for failure categories."""

    def test_config_error_exit_code(self, tmp_path: Path) -> None:
        """Config parse error exits with EXIT_CONFIG (2), not generic 1."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("run", "--config", str(tmp_path / "missing.toml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_no_adapters_exit_code(self, config_no_adapters: Path) -> None:
        """No enabled adapters exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("run", "--config", str(config_no_adapters))
        assert exc_info.value.code == EXIT_CONFIG

    def test_build_error_no_traceback(self, config_with_routes: Path) -> None:
        """Runtime build failure produces clean error, not raw traceback."""
        from unittest.mock import patch

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            side_effect=RuntimeError("simulated missing SDK dependency"),
        ):
            _, stderr = _run_cli_both(
                "run", "--config", str(config_with_routes)
            )
        assert "Traceback" not in stderr
        assert "Runtime build error:" in stderr

    def test_build_error_exit_code(self, config_with_routes: Path) -> None:
        """Runtime build failure exits with EXIT_BUILD (3)."""
        from medre.cli import EXIT_BUILD
        from unittest.mock import patch

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            side_effect=RuntimeError("simulated build failure"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("run", "--config", str(config_with_routes))
        assert exc_info.value.code == EXIT_BUILD

    def test_config_check_exit_code(self, tmp_path: Path) -> None:
        """Config check with missing file exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("config", "check", "--config", str(tmp_path / "missing.toml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_routes_validate_config_error_exit_code(self, tmp_path: Path) -> None:
        """Routes validate with missing config exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("routes", "validate", "--config", str(tmp_path / "missing.toml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_diagnostics_config_error_exit_code(self, tmp_path: Path) -> None:
        """Diagnostics with missing config exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("diagnostics", "--config", str(tmp_path / "missing.toml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_diagnostics_build_error_exit_code(self, config_with_routes: Path) -> None:
        """Diagnostics build failure exits with EXIT_BUILD (3)."""
        from medre.cli import EXIT_BUILD
        from unittest.mock import patch

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            side_effect=RuntimeError("simulated build failure"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("diagnostics", "--config", str(config_with_routes))
        assert exc_info.value.code == EXIT_BUILD

    def test_all_adapters_build_failure_exits_build(
        self, config_with_routes: Path
    ) -> None:
        """All adapters failing construction exits EXIT_BUILD (3), not EXIT_STARTUP (4)."""
        from medre.cli import EXIT_BUILD
        from unittest.mock import patch, MagicMock

        # Build a fake app with empty adapters and non-empty build_failures.
        fake_app = MagicMock()
        fake_app.adapters = {}  # all adapters failed to build
        fake_app.build_failures = [MagicMock(
            transport="matrix",
            adapter_id="main",
            error=RuntimeError("missing SDK"),
        )]

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("run", "--config", str(config_with_routes))
        assert exc_info.value.code == EXIT_BUILD

    def test_all_adapters_build_failure_no_traceback(
        self, config_with_routes: Path
    ) -> None:
        """All adapters build failure produces clean error, no traceback."""
        from unittest.mock import patch, MagicMock

        fake_app = MagicMock()
        fake_app.adapters = {}
        fake_app.build_failures = [MagicMock(
            transport="matrix",
            adapter_id="main",
            error=RuntimeError("missing SDK"),
        )]

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            _, stderr = _run_cli_both("run", "--config", str(config_with_routes))
        assert "Traceback" not in stderr
        assert "Runtime build error:" in stderr

    def test_all_adapters_start_failure_exits_startup(
        self, config_with_routes: Path
    ) -> None:
        """All adapters build OK but fail start() exits EXIT_STARTUP (4)."""
        from medre.cli import EXIT_STARTUP
        from unittest.mock import patch, MagicMock, AsyncMock
        from medre.runtime.errors import RuntimeStartupError

        fake_app = MagicMock()
        fake_app.adapters = {"main": MagicMock()}
        fake_app.build_failures = []
        fake_app.start = AsyncMock(
            side_effect=RuntimeStartupError(
                "Total startup failure: 0 of 1 adapter(s) started "
                "(1 start failed, 0 build failed)"
            )
        )

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("run", "--config", str(config_with_routes))
        assert exc_info.value.code == EXIT_STARTUP

    def test_startup_error_no_traceback(
        self, config_with_routes: Path
    ) -> None:
        """Startup failure produces clean error message, no traceback."""
        from unittest.mock import patch, MagicMock, AsyncMock
        from medre.runtime.errors import RuntimeStartupError

        fake_app = MagicMock()
        fake_app.adapters = {"main": MagicMock()}
        fake_app.build_failures = []
        fake_app.start = AsyncMock(
            side_effect=RuntimeStartupError(
                "Total startup failure: 0 of 1 adapter(s) started"
            )
        )

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            stdout, stderr = _run_cli_both("run", "--config", str(config_with_routes))
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr
        assert "Runtime startup failed:" in stdout

    def test_diagnostics_all_build_failure_exits_build(
        self, config_with_routes: Path
    ) -> None:
        """Diagnostics with all adapters failing construction exits EXIT_BUILD (3)."""
        from medre.cli import EXIT_BUILD
        from unittest.mock import patch, MagicMock

        fake_app = MagicMock()
        fake_app.adapters = {}
        fake_app.build_failures = [MagicMock(
            transport="matrix",
            adapter_id="main",
            error=RuntimeError("missing SDK"),
        )]

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("diagnostics", "--config", str(config_with_routes))
        assert exc_info.value.code == EXIT_BUILD

    def test_diagnostics_no_adapters_exits_config(
        self, config_no_adapters: Path
    ) -> None:
        """Diagnostics with zero enabled adapters exits EXIT_CONFIG (2), not EXIT_BUILD."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("diagnostics", "--config", str(config_no_adapters))
        assert exc_info.value.code == EXIT_CONFIG

    def test_diagnostics_no_adapters_clear_message(
        self, config_no_adapters: Path
    ) -> None:
        """Zero enabled adapters mentions 'no adapters enabled', not build failure."""
        _, stderr = _run_cli_both(
            "diagnostics", "--config", str(config_no_adapters)
        )
        assert "no adapters enabled" in stderr.lower()
        assert "failed to construct" not in stderr.lower()

    def test_diagnostics_partial_build_succeeds(
        self, config_with_routes: Path
    ) -> None:
        """Diagnostics with some adapters built, some failed, exits EXIT_OK (0)."""
        from unittest.mock import patch, MagicMock

        fake_app = MagicMock()
        fake_app.adapters = {"matrix.main": MagicMock()}
        fake_app.build_failures = [MagicMock(
            transport="matrix",
            adapter_id="backup",
            error=RuntimeError("missing SDK"),
        )]

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ), patch(
            "medre.runtime.snapshot.build_runtime_snapshot",
            return_value={"status": "degraded"},
        ):
            output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Secret redaction — config commands must not leak secrets
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    """Verify that CLI output does not contain access tokens or passwords."""

    def test_config_check_no_secrets(self, config_with_routes: Path) -> None:
        """Config check output must not contain access tokens."""
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        # The config has access_token = "tok" but output should not show it.
        assert "tok" not in output
        assert "access_token" not in output

    def test_routes_list_no_secrets(self, config_with_routes: Path) -> None:
        """Routes list output must not contain access tokens."""
        output = _run_cli("routes", "list", "--config", str(config_with_routes))
        assert "tok" not in output
        assert "access_token" not in output

    def test_routes_topology_no_secrets(self, config_with_routes: Path) -> None:
        """Routes topology output must not contain access tokens."""
        output = _run_cli("routes", "topology", "--config", str(config_with_routes))
        assert "tok" not in output
        assert "access_token" not in output


# ---------------------------------------------------------------------------
# Sample config parse + fake runtime assembly
# ---------------------------------------------------------------------------


class TestSampleConfigAndFakeRuntime:
    """Sample config parses correctly and a fake runtime can be assembled."""

    CONFIG_FAKE_MULTI = """\
[runtime]
name = "fake-runtime-test"

[logging]
level = "DEBUG"

[storage]
backend = "memory"

[adapters.matrix.fake_mx]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.test"
user_id = "@fake:fake.test"
access_token = "fake_token_for_test"
room_allowlist = ["!fake:fake.test"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "serial"
serial_port = "/dev/ttyFAKE"
meshnet_name = "FakeMesh"
"""

    @pytest.fixture()
    def fake_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "fake_config.toml"
        p.write_text(self.CONFIG_FAKE_MULTI)
        return p

    def test_sample_config_parses(self) -> None:
        """generate_sample_config() produces valid TOML."""
        from medre.config.sample import generate_sample_config
        import tomllib

        sample = generate_sample_config()
        parsed = tomllib.loads(sample)
        assert "runtime" in parsed
        assert "adapters" in parsed

    @pytest.mark.asyncio
    async def test_fake_runtime_assembles_from_config(
        self, fake_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A config with adapter_kind='fake' assembles into a working MedreApp."""
        from medre.config.loader import load_config
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.app import RuntimeState

        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        config, _source, paths = load_config(str(fake_config))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        assert len(app.adapters) == 2
        assert "fake_mx" in app.adapters
        assert "fake_mesh" in app.adapters
        assert app.state is RuntimeState.INITIALIZED

        await app.start()
        try:
            assert app.state is RuntimeState.RUNNING
            assert len(app.started_adapter_ids) == 2
        finally:
            await app.stop()
            assert app.state is RuntimeState.STOPPED


# ---------------------------------------------------------------------------
# medre diagnostics --refresh-health
# ---------------------------------------------------------------------------


class TestDiagnosticsRefreshHealth:
    """Tests for 'medre diagnostics --refresh-health' command."""

    CONFIG_FAKE_SINGLE = """\
[runtime]
name = "diag-refresh-test"

[storage]
backend = "memory"

[adapters.matrix.fake_main]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.test"
user_id = "@fake:fake.test"
access_token = "fake_token_for_test"
room_allowlist = ["!fake:fake.test"]
encryption_mode = "plaintext"
"""

    @pytest.fixture()
    def fake_single_config(self, tmp_path: Path) -> Path:
        """Write CONFIG_FAKE_SINGLE to a temp file and return its path."""
        p = tmp_path / "config.toml"
        p.write_text(self.CONFIG_FAKE_SINGLE)
        return p

    def test_refresh_health_produces_json(
        self, fake_single_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health with fake adapters produces parseable JSON."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics", "--refresh-health",
            "--config", str(fake_single_config),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_refresh_health_has_live_health(
        self, fake_single_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health populates health.live_health (not null)."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics", "--refresh-health",
            "--config", str(fake_single_config),
        )
        parsed = json.loads(output)
        health = parsed["health"]
        assert health["live_health"] is not None
        assert health["live_refresh"] is True
        assert health["scope"] == "live"

    def test_refresh_health_has_poll_count(
        self, fake_single_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health snapshot has poll_count=1 from single refresh."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics", "--refresh-health",
            "--config", str(fake_single_config),
        )
        parsed = json.loads(output)
        live_health = parsed["health"]["live_health"]
        assert live_health["poll_count"] == 1

    def test_refresh_health_has_adapter_entries(
        self, fake_single_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health populates per-adapter live health entries."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics", "--refresh-health",
            "--config", str(fake_single_config),
        )
        parsed = json.loads(output)
        adapters = parsed["health"]["live_health"]["adapters"]
        assert "fake_main" in adapters
        entry = adapters["fake_main"]
        assert entry["adapter_id"] == "fake_main"
        assert entry["health"] in ("healthy", "degraded", "failed", "unknown")
        assert "poll_timestamp_wall" in entry
        assert "poll_timestamp_monotonic" in entry
        assert "fake_or_live" in entry

    def test_refresh_health_startup_failure_exits_4(
        self, config_with_routes: Path,
    ) -> None:
        """--refresh-health startup failure exits EXIT_STARTUP (4)."""
        from medre.cli import EXIT_STARTUP
        from unittest.mock import patch, MagicMock, AsyncMock
        from medre.runtime.errors import RuntimeStartupError

        fake_app = MagicMock()
        fake_app.adapters = {"main": MagicMock()}
        fake_app.build_failures = []
        fake_app.start = AsyncMock(
            side_effect=RuntimeStartupError(
                "Total startup failure: 0 of 1 adapter(s) started"
            )
        )

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli(
                    "diagnostics", "--refresh-health",
                    "--config", str(config_with_routes),
                )
        assert exc_info.value.code == EXIT_STARTUP

    def test_refresh_health_startup_failure_no_traceback(
        self, config_with_routes: Path,
    ) -> None:
        """Startup failure produces clean error, no traceback."""
        from unittest.mock import patch, MagicMock, AsyncMock
        from medre.runtime.errors import RuntimeStartupError

        fake_app = MagicMock()
        fake_app.adapters = {"main": MagicMock()}
        fake_app.build_failures = []
        fake_app.start = AsyncMock(
            side_effect=RuntimeStartupError(
                "Total startup failure: 0 of 1 adapter(s) started"
            )
        )

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            return_value=fake_app,
        ):
            stdout, stderr = _run_cli_both(
                "diagnostics", "--refresh-health",
                "--config", str(config_with_routes),
            )
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr
        assert "Runtime startup failed:" in stderr

    def test_refresh_health_build_error_exits_3(
        self, config_with_routes: Path,
    ) -> None:
        """--refresh-health build failure exits EXIT_BUILD (3)."""
        from medre.cli import EXIT_BUILD
        from unittest.mock import patch

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            side_effect=RuntimeError("simulated build failure"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli(
                    "diagnostics", "--refresh-health",
                    "--config", str(config_with_routes),
                )
        assert exc_info.value.code == EXIT_BUILD

    def test_refresh_health_config_error_exits_2(self, tmp_path: Path) -> None:
        """--refresh-health with missing config exits EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "diagnostics", "--refresh-health",
                "--config", str(tmp_path / "missing.toml"),
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_refresh_health_no_adapters_exits_2(
        self, config_no_adapters: Path,
    ) -> None:
        """--refresh-health with no enabled adapters exits EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "diagnostics", "--refresh-health",
                "--config", str(config_no_adapters),
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_refresh_health_startup_health_remains_frozen(
        self, fake_single_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """startup.startup_health is frozen/separate from live health."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics", "--refresh-health",
            "--config", str(fake_single_config),
        )
        parsed = json.loads(output)
        # startup section exists with frozen startup_health
        assert "startup" in parsed
        startup = parsed["startup"]
        assert startup["scope"] == "startup"
        assert startup["live_refresh"] is False
        # health section is live
        health = parsed["health"]
        assert health["scope"] == "live"
        assert health["live_refresh"] is True
        assert health["live_health"] is not None

    def test_plain_diagnostics_unchanged_by_refresh_flag(
        self, config_with_routes: Path,
    ) -> None:
        """Plain 'medre diagnostics' (no --refresh-health) still works."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        # live_health should be null (no refresh)
        assert parsed["health"]["live_health"] is None
        assert parsed["health"]["live_refresh"] is False
        assert parsed["health"]["scope"] == "startup"


# ---------------------------------------------------------------------------
# Inspect command tests
# ---------------------------------------------------------------------------

CONFIG_INSPECT_SQLITE = """\
[runtime]
name = "test-inspect"

[storage]
backend = "sqlite"
path = "{state}/inspect.db"
"""

CONFIG_INSPECT_MEMORY = """\
[runtime]
name = "test-inspect-memory"

[storage]
backend = "memory"
"""


def _seed_inspect_db(
    db_path: str,
    event_id: str = "evt-inspect-1",
    source_adapter: str = "test_adapter",
    replay_run_id: str | None = None,
    native_adapter: str | None = None,
    native_channel_id: str | None = None,
    native_message_id: str | None = None,
) -> None:
    """Synchronously seed an inspect test database with an event + receipt + native ref.

    Runs the async seeding in a fresh event loop.
    """
    import asyncio
    from datetime import datetime, timezone
    from medre.core.events import (
        CanonicalEvent,
        DeliveryReceipt,
        EventMetadata,
        NativeMessageRef,
    )
    from medre.core.storage.sqlite import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = CanonicalEvent(
            event_id=event_id,
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
            source_adapter=source_adapter,
            source_transport_id="test-transport",
            source_channel_id="ch-inspect",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "inspect test message"},
            metadata=EventMetadata(),
        )
        await storage.append(event)

        receipt_kwargs: dict = dict(
            sequence=1,
            receipt_id="rcpt-inspect-1",
            event_id=event_id,
            delivery_plan_id="plan-inspect-1",
            target_adapter="dest_adapter",
            route_id="route-inspect",
            status="sent",
            created_at=datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc),
        )
        if replay_run_id is not None:
            receipt_kwargs["source"] = "replay"
            receipt_kwargs["replay_run_id"] = replay_run_id

        await storage.append_receipt(DeliveryReceipt(**receipt_kwargs))

        if native_adapter is not None and native_message_id is not None:
            await storage.store_native_ref(
                NativeMessageRef(
                    id="nref-inspect-1",
                    event_id=event_id,
                    adapter=native_adapter,
                    native_channel_id=native_channel_id,
                    native_message_id=native_message_id,
                    native_thread_id=None,
                    native_relation_id=None,
                    direction="outbound",
                    created_at=datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                )
            )

        await storage.close()

    asyncio.run(_seed())


@pytest.fixture()
def config_inspect_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage pointing at a temp MEDRE_HOME, with seeded DB."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(db_path)
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def config_inspect_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with memory backend."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_MEMORY)
    return p


@pytest.fixture()
def config_inspect_with_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Config with sqlite storage and seeded replay receipts."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-replay-1",
        replay_run_id="run-42",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def config_inspect_with_native_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Config with sqlite storage and seeded native ref."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-1",
        native_adapter="matrix",
        native_channel_id="!room:test",
        native_message_id="$native-msg-1",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


@pytest.fixture()
def config_inspect_native_null_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Config with sqlite storage and native ref with null channel."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / "state" / "inspect.db")
    _seed_inspect_db(
        db_path,
        event_id="evt-nref-nullch",
        native_adapter="meshtastic",
        native_channel_id=None,
        native_message_id="radio-msg-42",
    )
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_INSPECT_SQLITE)
    return p


class TestInspectParser:
    """Tests for 'medre inspect' argument parsing and dispatch."""

    def test_inspect_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect"])

    def test_inspect_unknown_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "nonexistent"])

    def test_inspect_event_requires_event_id(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "event", "--config", "/nonexistent/config.toml"])

    def test_inspect_receipts_requires_event_or_replay_run(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "receipts", "--config", "/nonexistent/config.toml"])

    def test_inspect_receipts_event_and_replay_run_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            main([
                "inspect", "receipts",
                "--event", "evt-1", "--replay-run", "run-1",
                "--config", "/nonexistent/config.toml",
            ])

    def test_inspect_native_ref_requires_adapter_and_message(self) -> None:
        with pytest.raises(SystemExit):
            main(["inspect", "native-ref", "--config", "/nonexistent/config.toml"])

    def test_inspect_native_ref_adapter_only_is_insufficient(self) -> None:
        with pytest.raises(SystemExit):
            main([
                "inspect", "native-ref",
                "--adapter", "matrix",
                "--config", "/nonexistent/config.toml",
            ])


class TestInspectEvent:
    """Tests for 'medre inspect event' command."""

    def test_event_found_returns_json(self, config_inspect_sqlite: Path) -> None:
        output = _run_cli(
            "inspect", "event", "evt-inspect-1",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-inspect-1"
        assert parsed["event_kind"] == "message.created"
        assert parsed["payload"]["text"] == "inspect test message"

    def test_event_json_is_deterministic(
        self, config_inspect_sqlite: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "event", "evt-inspect-1",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        # Keys are sorted (deterministic JSON)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_event_not_found_exits_not_found(
        self, config_inspect_sqlite: Path,
    ) -> None:
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect", "event", "nonexistent-event",
                "--config", str(config_inspect_sqlite),
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_event_not_found_stderr_message(
        self, config_inspect_sqlite: Path,
    ) -> None:
        stdout, stderr = _run_cli_both(
            "inspect", "event", "nonexistent-event",
            "--config", str(config_inspect_sqlite),
        )
        assert "event not found" in stderr
        assert "nonexistent-event" in stderr

    def test_event_memory_backend_exits_config(
        self, config_inspect_memory: Path,
    ) -> None:
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect", "event", "evt-1",
                "--config", str(config_inspect_memory),
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_event_memory_backend_stderr_message(
        self, config_inspect_memory: Path,
    ) -> None:
        stdout, stderr = _run_cli_both(
            "inspect", "event", "evt-1",
            "--config", str(config_inspect_memory),
        )
        assert "memory" in stderr


class TestInspectReceipts:
    """Tests for 'medre inspect receipts' command."""

    def test_receipts_by_event_found(
        self, config_inspect_sqlite: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "receipts", "--event", "evt-inspect-1",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["event_id"] == "evt-inspect-1"
        assert parsed[0]["receipt_id"] == "rcpt-inspect-1"
        assert parsed[0]["status"] == "sent"

    def test_receipts_by_event_empty_list(
        self, config_inspect_sqlite: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "receipts", "--event", "nonexistent-event",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 0

    def test_receipts_by_replay_run_found(
        self, config_inspect_with_replay: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "receipts", "--replay-run", "run-42",
            "--config", str(config_inspect_with_replay),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["source"] == "replay"
        assert parsed[0]["replay_run_id"] == "run-42"

    def test_receipts_by_replay_run_empty_list(
        self, config_inspect_sqlite: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "receipts", "--replay-run", "nonexistent-run",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 0

    def test_receipts_json_is_deterministic(
        self, config_inspect_sqlite: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "receipts", "--event", "evt-inspect-1",
            "--config", str(config_inspect_sqlite),
        )
        parsed = json.loads(output)
        receipt = parsed[0]
        keys = list(receipt.keys())
        assert keys == sorted(keys)

    def test_receipts_memory_backend_exits_config(
        self, config_inspect_memory: Path,
    ) -> None:
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect", "receipts", "--event", "evt-1",
                "--config", str(config_inspect_memory),
            )
        assert exc_info.value.code == EXIT_CONFIG


class TestInspectNativeRef:
    """Tests for 'medre inspect native-ref' command."""

    def test_native_ref_found_returns_event(
        self, config_inspect_with_native_ref: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "native-ref",
            "--adapter", "matrix",
            "--channel", "!room:test",
            "--message", "$native-msg-1",
            "--config", str(config_inspect_with_native_ref),
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-nref-1"
        assert parsed["adapter"] == "matrix"
        assert parsed["native_message_id"] == "$native-msg-1"
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-nref-1"

    def test_native_ref_null_channel(
        self, config_inspect_native_null_channel: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "native-ref",
            "--adapter", "meshtastic",
            "--message", "radio-msg-42",
            "--config", str(config_inspect_native_null_channel),
        )
        parsed = json.loads(output)
        assert parsed["event_id"] == "evt-nref-nullch"
        assert parsed["native_channel_id"] is None

    def test_native_ref_not_found_exits_not_found(
        self, config_inspect_sqlite: Path,
    ) -> None:
        from medre.cli import EXIT_NOT_FOUND

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect", "native-ref",
                "--adapter", "nonexistent",
                "--message", "nonexistent-msg",
                "--config", str(config_inspect_sqlite),
            )
        assert exc_info.value.code == EXIT_NOT_FOUND

    def test_native_ref_not_found_stderr_message(
        self, config_inspect_sqlite: Path,
    ) -> None:
        stdout, stderr = _run_cli_both(
            "inspect", "native-ref",
            "--adapter", "nonexistent",
            "--message", "nonexistent-msg",
            "--config", str(config_inspect_sqlite),
        )
        assert "native ref not found" in stderr

    def test_native_ref_json_is_deterministic(
        self, config_inspect_with_native_ref: Path,
    ) -> None:
        output = _run_cli(
            "inspect", "native-ref",
            "--adapter", "matrix",
            "--channel", "!room:test",
            "--message", "$native-msg-1",
            "--config", str(config_inspect_with_native_ref),
        )
        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_native_ref_memory_backend_exits_config(
        self, config_inspect_memory: Path,
    ) -> None:
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "inspect", "native-ref",
                "--adapter", "matrix",
                "--message", "msg-1",
                "--config", str(config_inspect_memory),
            )
        assert exc_info.value.code == EXIT_CONFIG
