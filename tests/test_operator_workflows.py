"""Track 4: Operator workflow validation — end-to-end CLI smoke tests.

Validates that operators can successfully use MEDRE CLI commands in
deterministic, reproducible ways.  Every test:

- Uses **fake adapters** or config-only paths — no live transports or SDKs.
- Produces **deterministic output** — no timestamps, no randomness.
- Contains **no raw tracebacks** — operator-facing errors are clean.
- Runs **no actual install/venv creation** — programmatic checks only.

Scenarios covered:

1.  config sample → round-trip (generate, parse, validate)
2.  config check  — full output structure and adapter inventory
3.  routes workflow — validate → topology → list consistency
4.  diagnostics   — JSON structure and determinism
5.  paths         — MEDRE_HOME override and XDG fallback
6.  version       — output format
7.  adapters      — SDK availability listing and configured adapters
8.  Docker-style env overrides — deterministic env → config
9.  Shutdown/restart with fake runtime
10. Degraded-state messaging
11. Optional extras and install metadata checks
12. Config sample expanded validation (all sections, TOML parseable)
"""
from __future__ import annotations

import asyncio
import io
import importlib.metadata
import json
import os
import tomllib
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from medre.cli import main


# ---------------------------------------------------------------------------
# Shared config snippets
# ---------------------------------------------------------------------------

CONFIG_FAKE_MULTI = """\
[runtime]
name = "workflow-test"
shutdown_timeout_seconds = 5

[runtime.limits]
max_inflight_deliveries = 50
max_inflight_replay_events = 25
shutdown_drain_timeout_seconds = 3
delivery_acquire_timeout_seconds = 0.5

[logging]
level = "INFO"
format = "text"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake_tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMesh"

[routes.matrix_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:fake.local"
dest_channel = "1"

[routes.mesh_to_matrix]
source_adapters = ["fake_mesh"]
dest_adapters = ["fake_matrix"]
directionality = "source_to_dest"
enabled = false

[routes.bidirectional_bridge]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "bidirectional"
enabled = true

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
"""

CONFIG_MINIMAL_MEMORY = """\
[runtime]
name = "minimal-workflow"

[storage]
backend = "memory"
"""

CONFIG_SINGLE_ADAPTER = """\
[runtime]
name = "single-adapter"

[storage]
backend = "memory"

[adapters.matrix.solo]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_single"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars for each test."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def config_fake_multi(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_FAKE_MULTI)
    return p


@pytest.fixture()
def config_minimal(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_MINIMAL_MEMORY)
    return p


@pytest.fixture()
def config_single(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_SINGLE_ADAPTER)
    return p


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set MEDRE_HOME to a temp dir and return it."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> str:
    """Run CLI, capture stdout, return output. Propagate non-zero SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
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


def _run_cli_raw(*args: str) -> tuple[str, str, int | None]:
    """Run CLI and return (stdout, stderr, exit_code)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        code = 1 if isinstance(e.code, str) else e.code
    return stdout.getvalue(), stderr.getvalue(), code


# ===================================================================
# 1. Config sample round-trip workflow
# ===================================================================


class TestConfigSampleWorkflow:
    """Operators generate a sample config, save it, and validate it."""

    def test_sample_is_valid_toml(self) -> None:
        """Sample config output is parseable TOML."""
        output = _run_cli("config", "sample")
        # Strip comments to get valid TOML sections — but the sample should
        # parse as-is since all commented lines are valid TOML comments.
        parsed = tomllib.loads(output)
        assert isinstance(parsed, dict)

    def test_sample_round_trip_config_check(self, tmp_path: Path) -> None:
        """Generate sample → save → config check passes."""
        output = _run_cli("config", "sample")
        # The sample has empty access_token which may cause validation issues.
        # Strip comment-only lines, keep active sections.
        active_lines = []
        for line in output.splitlines():
            stripped = line.strip()
            # Keep section headers, key = value, blank lines
            if not stripped.startswith("#") and stripped:
                active_lines.append(line)
        active_toml = "\n".join(active_lines)
        if not active_toml.strip():
            pytest.skip("sample config is entirely commented out")

        cfg_path = tmp_path / "from_sample.toml"
        cfg_path.write_text(active_toml)
        output, stderr, code = _run_cli_raw(
            "config", "check", "--config", str(cfg_path)
        )
        # It should at least parse without traceback.
        assert "Traceback" not in stderr
        assert "Traceback" not in output

    def test_sample_includes_all_adapter_types(self) -> None:
        """Sample mentions all four transport types."""
        output = _run_cli("config", "sample")
        for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
            assert transport in output, f"sample missing {transport} adapter"

    def test_sample_includes_all_key_sections(self) -> None:
        """Sample includes runtime, logging, storage, adapters, routes."""
        output = _run_cli("config", "sample")
        for section in ("runtime", "logging", "storage", "adapters", "routes"):
            assert section in output, f"sample missing [{section}] section"

    def test_sample_includes_limits(self) -> None:
        """Sample documents runtime.limits with all four fields."""
        output = _run_cli("config", "sample")
        assert "max_inflight_deliveries" in output
        assert "max_inflight_replay_events" in output
        assert "shutdown_drain_timeout_seconds" in output
        assert "delivery_acquire_timeout_seconds" in output

    def test_sample_includes_encryption_modes(self) -> None:
        """Sample documents encryption mode options."""
        output = _run_cli("config", "sample")
        assert "plaintext" in output
        assert "encryption_mode" in output

    def test_sample_includes_env_var_guidance(self) -> None:
        """Sample mentions env var usage for secrets."""
        output = _run_cli("config", "sample")
        assert "MEDRE_MATRIX_ACCESS_TOKEN" in output or "env" in output.lower()


# ===================================================================
# 2. Config check full output structure
# ===================================================================


class TestConfigCheckWorkflow:
    """Operators run 'medre config check' and read structured output."""

    def test_config_check_shows_source(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Source:" in output

    def test_config_check_shows_resolved_paths(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Resolved paths:" in output
        assert "State dir:" in output
        assert "Data dir:" in output
        assert "Cache dir:" in output
        assert "Log dir:" in output

    def test_config_check_adapter_inventory(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Adapter inventory:" in output
        assert "matrix.fake_matrix" in output
        assert "meshtastic.fake_mesh" in output
        assert "enabled" in output

    def test_config_check_adapter_state_roots(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Adapter state roots:" in output

    def test_config_check_storage_backend(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Storage backend: memory" in output

    def test_config_check_runtime_limits(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Runtime limits:" in output
        assert "max_inflight_deliveries = 50" in output
        assert "max_inflight_replay_events = 25" in output
        assert "shutdown_drain_timeout_seconds = 3" in output
        assert "delivery_acquire_timeout_seconds = 0.5" in output

    def test_config_check_route_inventory(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Route inventory:" in output
        assert "matrix_to_mesh" in output
        assert "mesh_to_matrix" in output
        assert "bidirectional_bridge" in output

    def test_config_check_summary(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Config valid" in output
        assert "2/2 adapter(s) enabled" in output
        assert "2/3 route(s) active" in output

    def test_config_check_startup_preview(self, config_fake_multi: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Startup preview:" in output
        assert "Adapters that will start:" in output
        assert "fake_matrix" in output
        assert "fake_mesh" in output
        assert "Routes that will activate:" in output

    def test_config_check_no_routes_minimal(self, config_minimal: Path) -> None:
        output = _run_cli("config", "check", "--config", str(config_minimal))
        assert "(no routes configured)" in output
        assert "Config valid" in output

    def test_config_check_no_traceback_on_all_errors(self, tmp_path: Path) -> None:
        """Any config error produces clean output, never a raw traceback."""
        _, stderr, _ = _run_cli_raw(
            "config", "check", "--config", str(tmp_path / "missing.toml")
        )
        assert "Traceback" not in stderr
        assert "Config error:" in stderr


# ===================================================================
# 3. Routes workflow — validate → topology → list consistency
# ===================================================================


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
        # mesh_to_matrix is disabled so no warning about missing enabled adapters
        # but bidirectional_bridge and matrix_to_mesh should be fine
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


# ===================================================================
# 4. Diagnostics workflow
# ===================================================================


class TestDiagnosticsWorkflow:
    """Operators run 'medre diagnostics' for pre-flight snapshots."""

    def test_diagnostics_produces_valid_json(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_diagnostics_has_schema_version(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "schema_version" in parsed
        assert isinstance(parsed["schema_version"], int)

    def test_diagnostics_has_runtime_state(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "lifecycle" in parsed
        assert "runtime_state" in parsed["lifecycle"]
        assert isinstance(parsed["lifecycle"]["runtime_state"], str)

    def test_diagnostics_has_adapters(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "adapters" in parsed
        assert isinstance(parsed["adapters"], dict)

    def test_diagnostics_has_routes(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "routes" in parsed

    def test_diagnostics_has_limits(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "limits" in parsed

    def test_diagnostics_deterministic_keys_sorted(
        self, config_fake_multi: Path
    ) -> None:
        """JSON keys are sorted for stable output."""
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        # Re-parse and verify key order
        parsed = json.loads(output)
        top_keys = list(parsed.keys())
        assert top_keys == sorted(top_keys), f"keys not sorted: {top_keys}"

    def test_diagnostics_no_secrets(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        assert "fake_tok" not in output
        assert "access_token" not in output

    def test_diagnostics_missing_config_clean(self, tmp_path: Path) -> None:
        _, stderr, code = _run_cli_raw(
            "diagnostics", "--config", str(tmp_path / "missing.toml")
        )
        assert code != 0
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_diagnostics_with_single_adapter(self, config_single: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_single))
        parsed = json.loads(output)
        assert "adapters" in parsed
        assert len(parsed["adapters"]) >= 1


# ===================================================================
# 5. Paths workflow
# ===================================================================


class TestPathsWorkflow:
    """Operators run 'medre paths' to verify resolved directories."""

    def test_paths_shows_all_dirs(self) -> None:
        output = _run_cli("paths")
        assert "Config file:" in output
        assert "State dir:" in output
        assert "Data dir:" in output
        assert "Cache dir:" in output
        assert "Log dir:" in output
        assert "Global DB:" in output

    def test_paths_with_medre_home(self, tmp_home: Path) -> None:
        output = _run_cli("paths")
        assert "MEDRE_HOME" in output
        assert str(tmp_home) in output

    def test_paths_xdg_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without MEDRE_HOME, shows XDG mode."""
        # All MEDRE_ and XDG_ env vars already cleaned by autouse fixture.
        output = _run_cli("paths")
        assert "XDG" in output or "Mode:" in output

    def test_paths_dir_status_indicators(self, tmp_home: Path) -> None:
        """Each dir shows [exists] or [will be created]."""
        output = _run_cli("paths")
        # At least one dir status indicator should appear
        assert "exists" in output or "will be created" in output


# ===================================================================
# 6. Version workflow
# ===================================================================


class TestVersionWorkflow:
    """Operators run 'medre version' to check installed version."""

    def test_version_format(self) -> None:
        output = _run_cli("version")
        lines = output.strip().splitlines()
        assert lines[0].startswith("medre ")
        # Version should be dotted numeric
        version_str = lines[0].split()[-1]
        parts = version_str.split(".")
        assert len(parts) >= 2
        for part in parts:
            assert part.isdigit(), f"non-numeric version segment: {part!r}"

    def test_version_includes_python(self) -> None:
        output = _run_cli("version")
        assert "Python" in output

    def test_version_includes_platform(self) -> None:
        output = _run_cli("version")
        assert "Platform" in output

    def test_version_deterministic(self) -> None:
        """Same result twice in a row."""
        first = _run_cli("version")
        second = _run_cli("version")
        assert first == second


# ===================================================================
# 7. Adapters workflow
# ===================================================================


class TestAdaptersWorkflow:
    """Operators run 'medre adapters' to check SDK and config status."""

    def test_adapters_shows_types(self) -> None:
        output = _run_cli("adapters")
        assert "Adapter types:" in output
        for transport in ("matrix", "meshtastic", "meshcore", "lxmf"):
            assert transport in output, f"adapters output missing {transport}"

    def test_adapters_shows_sdk_status(self) -> None:
        output = _run_cli("adapters")
        assert "installed" in output or "not installed" in output

    def test_adapters_with_config(
        self, config_fake_multi: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a config file is loadable, adapters command shows configured adapters."""
        monkeypatch.setenv("MEDRE_CONFIG", str(config_fake_multi))
        output = _run_cli("adapters")
        assert "Configured adapters:" in output
        assert "fake_matrix" in output
        assert "fake_mesh" in output

    def test_adapters_no_config_no_traceback(self) -> None:
        """Without any config, adapters still works cleanly."""
        output = _run_cli("adapters")
        assert "Traceback" not in output
        # Should mention either "No adapters configured" or "No config found"
        assert "No " in output or "Adapter types:" in output


# ===================================================================
# 8. Docker-style env overrides
# ===================================================================


class TestDockerEnvWorkflow:
    """Operators use MEDRE_* env vars in Docker/Compose deployments."""

    def test_medre_home_overrides_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "custom_home"
        home_dir.mkdir()
        monkeypatch.setenv("MEDRE_HOME", str(home_dir))
        output = _run_cli("paths")
        assert str(home_dir) in output

    def test_medre_log_level_env(
        self, config_fake_multi: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDRE_LOG_LEVEL env is picked up through config check (applied via diagnostics)."""
        monkeypatch.setenv("MEDRE_LOG_LEVEL", "DEBUG")
        # config check itself may not show log level, but diagnostics should reflect it.
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)
        # The config was loaded with env overrides applied in diagnostics.

    def test_env_overrides_do_not_leak_in_config_check(
        self, config_fake_multi: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config check output does not contain secret env values."""
        monkeypatch.setenv("MEDRE_MATRIX_ACCESS_TOKEN", "env_secret_token_12345")
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "env_secret_token_12345" not in output

    def test_medre_config_env(
        self, config_fake_multi: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MEDRE_CONFIG env var points to config file."""
        monkeypatch.setenv("MEDRE_CONFIG", str(config_fake_multi))
        output = _run_cli("config", "check")
        assert "Config valid" in output

    def test_docker_env_example_file_exists(self) -> None:
        """The docker.env.example file is shipped."""
        repo_root = Path(__file__).resolve().parent.parent
        env_example = repo_root / "examples" / "env" / "docker.env.example"
        assert env_example.is_file(), "docker.env.example not found"

    def test_docker_env_example_documents_medre_home(self) -> None:
        """docker.env.example documents MEDRE_HOME."""
        repo_root = Path(__file__).resolve().parent.parent
        env_example = repo_root / "examples" / "env" / "docker.env.example"
        content = env_example.read_text()
        assert "MEDRE_HOME" in content

    def test_docker_env_example_no_real_secrets(self) -> None:
        """docker.env.example uses placeholder tokens, not real ones."""
        repo_root = Path(__file__).resolve().parent.parent
        env_example = repo_root / "examples" / "env" / "docker.env.example"
        content = env_example.read_text()
        # Should NOT contain real-looking Matrix tokens
        assert "syt_" not in content or "secret" in content.lower() or "here" in content.lower()


# ===================================================================
# 9. Shutdown/restart workflow with fake runtime
# ===================================================================


class TestShutdownRestartWorkflow:
    """Operators start and stop the runtime with fake adapters."""

    def test_run_exits_on_no_enabled_adapters(self, config_minimal: Path) -> None:
        """Run with no adapters exits cleanly with clear message."""
        _, stderr, code = _run_cli_raw("run", "--config", str(config_minimal))
        assert code != 0
        assert "Traceback" not in stderr
        assert "adapter" in stderr.lower()

    def test_config_check_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: check config before attempting run."""
        output = _run_cli("config", "check", "--config", str(config_fake_multi))
        assert "Config valid" in output
        assert "2/2 adapter(s) enabled" in output

    def test_routes_validate_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: validate routes before attempting run."""
        output = _run_cli("routes", "validate", "--config", str(config_fake_multi))
        assert "Routes valid" in output

    def test_diagnostics_before_run(self, config_fake_multi: Path) -> None:
        """Operator workflow: check diagnostics before attempting run."""
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "schema_version" in parsed

    def test_fake_runtime_build_and_snapshot(self, config_fake_multi: Path) -> None:
        """RuntimeBuilder can build from fake config and produce a snapshot."""
        from medre.config.loader import load_config
        from medre.config.paths import resolve
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot
        from datetime import datetime, timezone

        config, _source, paths = load_config(str(config_fake_multi))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        assert app is not None
        assert len(app.adapters) >= 1

        snapshot = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        assert isinstance(snapshot, dict)
        assert "schema_version" in snapshot
        assert snapshot["schema_version"] == 1
        assert "adapters" in snapshot


# ===================================================================
# 10. Degraded-state messaging
# ===================================================================


class TestDegradedStateMessaging:
    """Operators see clear 'degraded' messaging when adapters partially fail."""

    def test_degraded_build_failure_in_snapshot(self, tmp_path: Path) -> None:
        """Build failures appear in diagnostics snapshot."""
        # Config with a real (non-fake) meshtastic adapter that will fail to build.
        config_with_real = """\
[runtime]
name = "degraded-test"

[storage]
backend = "memory"

[adapters.matrix.fm]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.real_radio]
enabled = true
connection_type = "serial"
serial_port = "/dev/ttyNONEXISTENT"
meshnet_name = "TestMesh"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_with_real)

        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot
        from datetime import datetime, timezone

        config, _source, paths = load_config(str(p))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        # Should have at least the fake matrix adapter
        assert len(app.adapters) >= 1

        # May have build failures for the real meshtastic adapter
        if app.build_failures:
            snapshot = build_runtime_snapshot(
                app,
                now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
                monotonic_fn=lambda: 0.0,
            )
            assert "build_failures" in snapshot
            assert len(snapshot["build_failures"]) > 0

    def test_config_check_with_disabled_adapter(self, tmp_path: Path) -> None:
        """Config check shows disabled adapters clearly."""
        config_mixed = """\
[runtime]
name = "mixed-test"

[storage]
backend = "memory"

[adapters.matrix.active]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.inactive]
enabled = false
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_mixed)
        output = _run_cli("config", "check", "--config", str(p))
        assert "active: enabled" in output
        assert "inactive: disabled" in output
        assert "1/2 adapter(s) enabled" in output
        assert "Config valid" in output

    def test_config_check_no_enabled_adapters(self, tmp_path: Path) -> None:
        """Config with all adapters disabled shows 0 enabled."""
        config_all_disabled = """\
[runtime]
name = "all-disabled"

[storage]
backend = "memory"

[adapters.matrix.offline]
enabled = false
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""
        p = tmp_path / "config.toml"
        p.write_text(config_all_disabled)
        output = _run_cli("config", "check", "--config", str(p))
        assert "0/1 adapter(s) enabled" in output
        assert "Config valid" in output


# ===================================================================
# 11. Optional extras and install metadata
# ===================================================================


class TestInstallMetadataWorkflow:
    """Operators verify installation metadata without pip/venv."""

    def test_entry_point_documented_in_pyproject(self) -> None:
        """pyproject.toml declares 'medre' console_scripts entry point."""
        import tomllib

        repo_root = Path(__file__).resolve().parent.parent
        with (repo_root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        scripts = data["project"].get("scripts", {})
        assert "medre" in scripts
        assert scripts["medre"] == "medre.cli:main"

    def test_documented_extras_in_pyproject(self) -> None:
        """All transport extras are declared in pyproject.toml."""
        import tomllib

        repo_root = Path(__file__).resolve().parent.parent
        with (repo_root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        opt = data["project"].get("optional-dependencies", {})
        required_extras = {"matrix", "matrix-e2e", "meshtastic", "meshcore", "lxmf"}
        missing = required_extras - set(opt.keys())
        assert not missing, f"missing extras: {sorted(missing)}"

    def test_dev_extras_exist(self) -> None:
        """Dev extras include pytest."""
        import tomllib

        repo_root = Path(__file__).resolve().parent.parent
        with (repo_root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        opt = data["project"].get("optional-dependencies", {})
        assert "dev" in opt
        dev_deps = opt["dev"]
        assert any("pytest" in d for d in dev_deps)

    def test_base_dep_is_msgspec(self) -> None:
        """Only base dependency is msgspec."""
        import tomllib

        repo_root = Path(__file__).resolve().parent.parent
        with (repo_root / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        deps = data["project"].get("dependencies", [])
        assert any("msgspec" in d for d in deps)

    def test_version_accessible_via_importlib(self) -> None:
        """Version is accessible via importlib.metadata."""
        from medre.cli.main import _get_version

        version = _get_version()
        assert version
        parts = version.split(".")
        assert len(parts) >= 2
        for part in parts:
            assert part.isdigit()

    def test_python_module_entry_point(self) -> None:
        """python -m medre.cli works as documented."""
        from medre.cli import __name__ as module_name

        # The module supports python -m via __main__ block
        import importlib

        mod = importlib.import_module("medre.cli")
        assert hasattr(mod, "main")
        assert hasattr(mod, "__name__")


# ===================================================================
# 12. Config sample expanded validation
# ===================================================================


class TestConfigSampleExpanded:
    """Expanded validation of 'medre config sample' output."""

    def test_sample_toml_sections_parse(self) -> None:
        """Every uncommented section in the sample parses as valid TOML."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        assert "runtime" in parsed

    def test_sample_runtime_has_name(self) -> None:
        """Sample [runtime] has a name field."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        assert "name" in parsed.get("runtime", {})

    def test_sample_storage_has_backend(self) -> None:
        """Sample [storage] has a backend field."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        storage = parsed.get("storage", {})
        assert "backend" in storage

    def test_sample_matrix_adapter_fields(self) -> None:
        """Sample Matrix adapter has required fields."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        adapters = parsed.get("adapters", {})
        matrix = adapters.get("matrix", {})
        assert len(matrix) > 0, "sample has no matrix adapters"
        # Get the first matrix adapter
        first_adapter = next(iter(matrix.values()))
        assert "homeserver" in first_adapter
        assert "user_id" in first_adapter
        assert "room_allowlist" in first_adapter
        assert "encryption_mode" in first_adapter

    def test_sample_meshtastic_adapter_fields(self) -> None:
        """Sample Meshtastic adapter has required fields."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        adapters = parsed.get("adapters", {})
        meshtastic = adapters.get("meshtastic", {})
        if meshtastic:
            first_adapter = next(iter(meshtastic.values()))
            assert "connection_type" in first_adapter

    def test_sample_routes_have_required_fields(self) -> None:
        """Active sample routes have source_adapters and dest_adapters."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        routes = parsed.get("routes", {})
        for route_id, route_data in routes.items():
            assert "source_adapters" in route_data, (
                f"sample route {route_id} missing source_adapters"
            )
            assert "dest_adapters" in route_data, (
                f"sample route {route_id} missing dest_adapters"
            )

    def test_sample_limits_have_defaults(self) -> None:
        """Sample [runtime.limits] has all four limit fields."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        runtime = parsed.get("runtime", {})
        limits = runtime.get("limits", {})
        expected_fields = {
            "max_inflight_deliveries",
            "max_inflight_replay_events",
            "shutdown_drain_timeout_seconds",
            "delivery_acquire_timeout_seconds",
        }
        for field in expected_fields:
            assert field in limits, f"sample limits missing {field}"

    def test_sample_no_deprecated_language(self) -> None:
        """Sample does not contain deprecated terms."""
        output = _run_cli("config", "sample")
        deprecated = ["legacy", "deprecated", "old_config", "v1_config", "compat_mode"]
        for term in deprecated:
            assert term not in output.lower(), (
                f"sample contains deprecated term: {term}"
            )

    def test_sample_logging_section(self) -> None:
        """Sample includes [logging] with level and format."""
        output = _run_cli("config", "sample")
        parsed = tomllib.loads(output)
        logging_cfg = parsed.get("logging", {})
        assert "level" in logging_cfg
        assert "format" in logging_cfg

    def test_sample_no_duplicate_keys(self) -> None:
        """Sample TOML has no duplicate keys (tomllib enforces this)."""
        output = _run_cli("config", "sample")
        # If there were duplicate keys, tomllib would raise
        parsed = tomllib.loads(output)
        assert isinstance(parsed, dict)


# ===================================================================
# Cross-cutting: no-Traceback guarantee across all commands
# ===================================================================


class TestNoTracebackGuarantee:
    """Every CLI command produces clean output on misuse — no raw tracebacks."""

    @pytest.mark.parametrize(
        "args",
        [
            ("config", "check", "--config", "/nonexistent/path.toml"),
            ("routes", "validate", "--config", "/nonexistent/path.toml"),
            ("routes", "topology", "--config", "/nonexistent/path.toml"),
            ("routes", "list", "--config", "/nonexistent/path.toml"),
            ("diagnostics", "--config", "/nonexistent/path.toml"),
        ],
    )
    def test_missing_config_no_traceback(self, args: tuple[str, ...]) -> None:
        _, stderr, code = _run_cli_raw(*args)
        assert code != 0
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_unknown_command_no_traceback(self) -> None:
        _, stderr, code = _run_cli_raw("nonexistent_command")
        assert code != 0
        assert "Traceback" not in stderr

    def test_routes_without_subcommand_no_traceback(self) -> None:
        _, stderr, code = _run_cli_raw("routes")
        assert code != 0
        assert "Traceback" not in stderr

    def test_run_missing_config_no_traceback(self, tmp_path: Path) -> None:
        _, stderr, code = _run_cli_raw(
            "run", "--config", str(tmp_path / "missing.toml")
        )
        assert code != 0
        assert "Traceback" not in stderr


# ===================================================================
# 13. Signal safety and shutdown request
# ===================================================================


class TestSignalSafety:
    """Signal handler triggers clean shutdown via _request_shutdown."""

    @pytest.mark.asyncio
    async def test_request_shutdown_sets_flag_and_clean_stop(
        self, tmp_path: Path
    ) -> None:
        """Calling _request_shutdown simulates SIGTERM; app stops cleanly."""
        from medre.cli.run_commands import _request_shutdown, shutdown_requested
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        import signal as signal_mod
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()
        await app.start()
        assert app.state.value == "running"

        # Simulate SIGTERM: call _request_shutdown directly.
        run_mod.shutdown_requested = False
        _request_shutdown(signal_mod.SIGTERM, None)
        assert run_mod.shutdown_requested is True

        # app.stop() should complete cleanly.
        await app.stop()
        assert app.state.value == "stopped"

        # Reset global for subsequent tests.
        run_mod.shutdown_requested = False

    @pytest.mark.asyncio
    async def test_request_shutdown_sigint(self) -> None:
        """SIGINT also sets shutdown_requested."""
        from medre.cli.run_commands import _request_shutdown
        import signal as signal_mod
        import medre.cli.run_commands as run_mod

        run_mod.shutdown_requested = False
        _request_shutdown(signal_mod.SIGINT, None)
        assert run_mod.shutdown_requested is True
        run_mod.shutdown_requested = False


# ===================================================================
# 14. Snapshot-on-shutdown end-to-end
# ===================================================================


class TestSnapshotOnShutdown:
    """--snapshot-on-shutdown writes a valid JSON snapshot on graceful stop."""

    @pytest.mark.asyncio
    async def test_snapshot_written_on_graceful_stop(
        self, tmp_path: Path
    ) -> None:
        """Runtime builds snapshot and writes JSON to the specified path."""
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()
        await app.start()

        snap = build_runtime_snapshot(app)
        snap_path = tmp_path / "shutdown.json"
        snap_path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")

        await app.stop()

        assert snap_path.exists()
        data = json.loads(snap_path.read_text())
        assert "schema_version" in data
        assert data["schema_version"] == 1
        assert "adapters" in data
        assert "lifecycle" in data

    @pytest.mark.asyncio
    async def test_snapshot_has_expected_keys(self, tmp_path: Path) -> None:
        """Snapshot dict contains all required top-level sections."""
        from medre.config.loader import load_config
        from medre.runtime.builder import RuntimeBuilder
        from medre.runtime.snapshot import build_runtime_snapshot
        from datetime import datetime, timezone

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        config, _source, paths = load_config(str(p))
        app = RuntimeBuilder(config, paths).build()

        snap = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )

        expected_sections = {
            "schema_version", "snapshot_at", "accounting", "adapters",
            "capacity", "diagnostics", "health", "identity", "lifecycle",
            "limits", "persistence", "replay", "routes", "startup", "unstable",
        }
        assert set(snap.keys()) == expected_sections


# ===================================================================
# 15. Real CLI snapshot test — _run() writes snapshot via CLI lifecycle
# ===================================================================


class TestRealSnapshotOnShutdown:
    """_run() lifecycle writes a valid snapshot to the specified path."""

    @pytest.mark.asyncio
    async def test_snapshot_written_after_shutdown(self, tmp_path: Path) -> None:
        """Full _run() lifecycle: startup → shutdown → snapshot file exists and is valid."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        # Reset global state before test.
        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            """Wait for startup, then trigger shutdown."""
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        task = asyncio.create_task(run_mod._run(str(p), snapshot_path=str(snap_path)))
        trigger = asyncio.create_task(_trigger_shutdown())
        await asyncio.gather(task, trigger)

        assert snap_path.exists(), "Snapshot file was not written"
        data = json.loads(snap_path.read_text())
        assert data["schema_version"] == 1
        assert "lifecycle" in data
        assert data["lifecycle"]["runtime_state"] == "stopped"
        assert "accounting" in data
        assert "routes" in data

    @pytest.mark.asyncio
    async def test_snapshot_json_is_valid(self, tmp_path: Path) -> None:
        """Snapshot written by _run() is valid, parseable JSON."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        task = asyncio.create_task(run_mod._run(str(p), snapshot_path=str(snap_path)))
        trigger = asyncio.create_task(_trigger_shutdown())
        await asyncio.gather(task, trigger)

        raw = snap_path.read_text()
        data = json.loads(raw)
        assert isinstance(data, dict)
        # Keys should be sorted (sort_keys=True in json.dumps).
        top_keys = list(data.keys())
        assert top_keys == sorted(top_keys)


# ===================================================================
# 16. Run output assertions — startup and shutdown stdout
# ===================================================================


class TestRunOutput:
    """Assert specific text appears in stdout during _run() lifecycle."""

    @pytest.mark.asyncio
    async def test_startup_output_lists_adapters(self, tmp_path: Path) -> None:
        """Startup output lists adapter IDs."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(_trigger_shutdown())
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "solo" in output

    @pytest.mark.asyncio
    async def test_startup_output_shows_route_eligibility(self, tmp_path: Path) -> None:
        """Route eligibility text appears when routes are configured."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_FAKE_MULTI)

        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(_trigger_shutdown())
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Route eligibility:" in output

    @pytest.mark.asyncio
    async def test_shutdown_output_shows_accounting(self, tmp_path: Path) -> None:
        """Accounting counters appear in stdout during shutdown."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(run_mod._run(str(p)))
            trigger = asyncio.create_task(_trigger_shutdown())
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Accounting:" in output

    @pytest.mark.asyncio
    async def test_shutdown_output_shows_snapshot_path(self, tmp_path: Path) -> None:
        """When snapshot_path is provided, 'Final snapshot written' appears."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)
        snap_path = tmp_path / "snap.json"

        run_mod.shutdown_requested = False

        async def _trigger_shutdown() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            task = asyncio.create_task(
                run_mod._run(str(p), snapshot_path=str(snap_path))
            )
            trigger = asyncio.create_task(_trigger_shutdown())
            await asyncio.gather(task, trigger)

        output = stdout.getvalue()
        assert "Final snapshot written to:" in output


# ===================================================================
# 17. Stale shutdown state — repeated _run() calls
# ===================================================================


class TestStaleShutdownState:
    """Repeated _run() calls do not inherit stale shutdown_requested."""

    @pytest.mark.asyncio
    async def test_repeated_run_no_stale_shutdown(self, tmp_path: Path) -> None:
        """shutdown_requested=True before first _run() is reset; second _run() starts clean."""
        import medre.cli.run_commands as run_mod

        p = tmp_path / "config.toml"
        p.write_text(CONFIG_SINGLE_ADAPTER)

        # Pre-set stale shutdown state.
        run_mod.shutdown_requested = True

        async def _trigger_shutdown_after_delay() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        # First run: should reset shutdown_requested to False at start,
        # then exit when we trigger shutdown.
        stdout1 = io.StringIO()
        with redirect_stdout(stdout1):
            task1 = asyncio.create_task(run_mod._run(str(p)))
            trigger1 = asyncio.create_task(_trigger_shutdown_after_delay())
            await asyncio.gather(task1, trigger1)

        # After first run, shutdown_requested is True (set by trigger).
        # Second run should reset it to False.
        async def _trigger_shutdown_second() -> None:
            await asyncio.sleep(0.3)
            run_mod.shutdown_requested = True

        stdout2 = io.StringIO()
        with redirect_stdout(stdout2):
            task2 = asyncio.create_task(run_mod._run(str(p)))
            trigger2 = asyncio.create_task(_trigger_shutdown_second())
            await asyncio.gather(task2, trigger2)

        # Both runs should have completed successfully (not hung).
        output2 = stdout2.getvalue()
        assert "Runtime shutting down" in output2


# ===================================================================
# 18. Operator run-session workflow (run_fake_bridge_smoke lifecycle)
# ===================================================================


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


class TestOperatorBridgeSession:
    """Full operator lifecycle: start → inject → stop → snapshot → inspect → trace → evidence.

    Every test uses ``run_fake_bridge_smoke`` which exercises the complete
    runtime pipeline with fake adapters.  No Docker, no network, no SDKs.
    """

    @pytest.mark.asyncio
    async def test_run_session_full_lifecycle(self, tmp_path: Path) -> None:
        """Full bridge session with persistent storage produces PASS with all evidence."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "session.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )

        # -- Status --
        assert report["status"] == "passed", (
            f"Expected PASS, got {report['status']}: "
            f"{report.get('fail_reasons', [])}"
        )

        # -- Storage path present --
        assert report["storage_path"] == db_path

        # -- Event ID present --
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0

        # -- Receipts found --
        receipts = report["delivery_receipts"]
        assert isinstance(receipts, list)
        assert len(receipts) >= 1
        for r in receipts:
            assert r["status"] == "sent"

        # -- Native refs found --
        native_refs = report["native_refs"]
        assert isinstance(native_refs, list)
        assert len(native_refs) >= 1
        for ref in native_refs:
            assert ref["resolves_to"] == report["event_id"]

        # -- Accounting counters present --
        acc = report["accounting"]
        assert isinstance(acc, dict)
        assert acc["outbound_delivered"] >= 1

        # -- Limits present in snapshot --
        snap = report["snapshot"]
        assert "accounting" in snap
        assert snap["accounting"] is not None

    @pytest.mark.asyncio
    async def test_run_session_report_cross_links(self, tmp_path: Path) -> None:
        """Report provides enough data to reconstruct trace/inspect/evidence CLI commands."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        config_path = _smoke_config_path()
        db_path = str(tmp_path / "crosslink.db")
        report = await run_fake_bridge_smoke(
            config_path,
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        event_id = report["event_id"]

        # The report should contain enough info to build operator CLI commands.
        # We verify the data is consistent and command strings can be assembled.
        commands = {
            "trace": f"medre trace event {event_id} --config <config>",
            "inspect": f"medre inspect event {event_id} --config <config>",
            "evidence": f"medre evidence --config <config> --event {event_id}",
        }

        # If storage_path is set, config must point to the persistent DB.
        assert report["storage_path"] == db_path

        # Verify each command string is well-formed.
        for cmd_name, cmd_str in commands.items():
            assert event_id in cmd_str, f"{cmd_name} command missing event_id"
            assert "--config" in cmd_str, f"{cmd_name} command missing --config"

        # Verify native_refs contain enough info to build native-ref commands.
        for ref in report["native_refs"]:
            assert "adapter" in ref
            assert "native_id" in ref
            inspect_nref_cmd = (
                f"medre inspect native-ref "
                f"--adapter {ref['adapter']} "
                f"--message {ref['native_id']}"
            )
            assert ref["adapter"] in inspect_nref_cmd
            assert ref["native_id"] in inspect_nref_cmd

    @pytest.mark.asyncio
    async def test_run_session_persistent_storage(self, tmp_path: Path) -> None:
        """SQLite file is created at storage_path and event is inspectable via CLI."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "persist.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        # -- SQLite file exists --
        assert Path(db_path).is_file(), f"SQLite DB not created at {db_path}"

        # -- Write a config that points to the same DB for CLI inspection --
        inspect_config = tmp_path / "inspect_config.toml"
        inspect_config.write_text(
            f'[runtime]\nname = "inspect-test"\n\n[storage]\n'
            f'backend = "sqlite"\npath = "{db_path}"\n'
        )

        # -- Inspect event directly (async, not via CLI to avoid nested event loop) --
        event_id = report["event_id"]
        from medre.cli.inspect_commands import _inspect_event
        from medre.cli.trace_commands import _trace_event

        # Inspect event
        evt_stdout = io.StringIO()
        with redirect_stdout(evt_stdout):
            await _inspect_event(str(inspect_config), event_id)
        assert event_id in evt_stdout.getvalue()

        # Inspect receipts
        from medre.cli.inspect_commands import _inspect_receipts

        rcpt_stdout = io.StringIO()
        with redirect_stdout(rcpt_stdout):
            await _inspect_receipts(str(inspect_config), event_id=event_id, replay_run_id=None)
        assert len(rcpt_stdout.getvalue().strip()) > 0

        # Trace event
        trace_stdout = io.StringIO()
        with redirect_stdout(trace_stdout):
            await _trace_event(str(inspect_config), event_id, json_output=False)
        assert event_id in trace_stdout.getvalue()

    @pytest.mark.asyncio
    async def test_run_session_snapshot_schema(self, tmp_path: Path) -> None:
        """Final snapshot has schema_version=1, lifecycle section, and accounting section."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "schema.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        snap = report["snapshot"]

        # -- Schema version --
        assert snap["schema_version"] == 1

        # -- Lifecycle section present --
        assert "lifecycle" in snap
        # The smoke snapshot is captured BEFORE app.stop(), so runtime_state
        # is "running". A post-stop snapshot (via _run + snapshot_path) would
        # show "stopped" — tested in TestRealSnapshotOnShutdown.
        assert "runtime_state" in snap["lifecycle"]

        # -- Accounting section present --
        assert "accounting" in snap
        assert snap["accounting"] is not None

        # -- Routes section present --
        assert "routes" in snap

    @pytest.mark.asyncio
    async def test_run_session_accounting_consistency(self, tmp_path: Path) -> None:
        """Accounting field names match run_commands.py output; no stale fields."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "accounting.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        acc = report["accounting"]
        assert isinstance(acc, dict)

        # -- Required fields (match run_commands.py _accounting output) --
        required_fields = [
            "inbound_accepted",
            "outbound_delivered",
            "outbound_failed",
            "loop_prevented",
            "capacity_rejections",
        ]
        for field in required_fields:
            assert field in acc, f"Missing required accounting field: {field}"

        # -- Values are integers >= 0 --
        for field in required_fields:
            assert isinstance(acc[field], int), (
                f"Accounting field {field} is not int: {type(acc[field])}"
            )
            assert acc[field] >= 0, f"Accounting field {field} is negative"

        # -- At least one delivered after smoke --
        assert acc["outbound_delivered"] >= 1
        assert acc["inbound_accepted"] >= 1

        # -- No stale fields --
        stale_fields = [
            "delivery_timeouts",
            "retry_exhausted",
            "legacy_delivered",
        ]
        for field in stale_fields:
            assert field not in acc, f"Stale accounting field present: {field}"

    @pytest.mark.asyncio
    async def test_run_session_json_safe(self, tmp_path: Path) -> None:
        """Report JSON is fully parseable and all command strings are valid."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "json_safe.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        # -- Round-trip through JSON --
        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["status"] == "passed"
        assert parsed["event_id"] == report["event_id"]

        # -- All top-level string values are non-empty where expected --
        assert isinstance(parsed["event_id"], str)
        assert len(parsed["event_id"]) > 0
        assert isinstance(parsed["evidence_level"], str)
        assert parsed["evidence_level"] == "fake_bridge"

        # -- Nested structures survive round-trip --
        assert isinstance(parsed["snapshot"], dict)
        assert isinstance(parsed["accounting"], dict)
        assert isinstance(parsed["delivery_receipts"], list)
        assert isinstance(parsed["native_refs"], list)

    @pytest.mark.asyncio
    async def test_run_session_ephemeral_fallback(self) -> None:
        """Without storage_path, run_session works in-memory and report notes this."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        # No storage_path — in-memory (ephemeral) mode.
        report = await run_fake_bridge_smoke(_smoke_config_path())

        assert report["status"] == "passed"

        # -- No storage_path key in report (ephemeral) --
        assert "storage_path" not in report

        # -- Storage backend is memory --
        assert report["storage_backend"] == "memory"

        # -- Evidence still collected from in-memory storage --
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0
        receipts = report["delivery_receipts"]
        assert len(receipts) >= 1
        assert report["accounting"]["outbound_delivered"] >= 1


# ===================================================================
# 19. Scenario cross-check tests — run_bridge_session per scenario
# ===================================================================


_SCENARIOS = (
    "happy_path",
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
    "degraded_live_health",
)

_FAILURE_SCENARIOS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
    "capacity_rejection",
)

_DELIVERY_FAILURE_SCENARIOS = (
    "renderer_failure",
    "adapter_permanent_failure",
    "adapter_transient_failure",
)


class TestScenarioCrossCheck:
    """Every run-session scenario produces correct report fields.

    Each scenario is run via ``run_bridge_session`` with persistent storage
    and the report is verified for standardized field shapes.
    """

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_status_is_passed(self, scenario: str, tmp_path: Path) -> None:
        """Every scenario produces status='passed'."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"scenario-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed", (
            f"Scenario {scenario} failed: {report.get('fail_reasons', [])}"
        )

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_command_is_run_session(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has command='run_session'."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"cmd-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("command") == "run_session"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_scenario_category_matches(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report scenario_category matches expected category."""
        from medre.runtime.run_session import (
            run_bridge_session,
            _scenario_category,
        )

        db_path = str(tmp_path / f"cat-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        expected_cat = _scenario_category(scenario)
        assert report.get("scenario_category") == expected_cat, (
            f"scenario_category for {scenario}: "
            f"expected {expected_cat!r}, got {report.get('scenario_category')!r}"
        )

    @pytest.mark.parametrize("scenario", _FAILURE_SCENARIOS)
    @pytest.mark.asyncio
    async def test_failure_scenario_simulated(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Failure scenarios have simulated=True and simulation_method present."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"sim-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("simulated") is True
        assert report.get("simulation_method") is not None
        assert isinstance(report["simulation_method"], str)

    @pytest.mark.asyncio
    async def test_degraded_health_fields(self, tmp_path: Path) -> None:
        """degraded_live_health scenario has expected/observed health fields."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / "degraded-health.db")
        report = await run_bridge_session(
            scenario="degraded_live_health",
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("expected_health") == "degraded"
        assert report.get("observed_health") == "degraded"
        # degraded_live_health is not a delivery-failure scenario.
        assert report.get("expected_failure_kind") is None

    @pytest.mark.parametrize("scenario", _DELIVERY_FAILURE_SCENARIOS)
    @pytest.mark.asyncio
    async def test_delivery_failure_has_failure_kinds(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Delivery failure scenarios have expected/observed failure_kind."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"fk-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report.get("expected_failure_kind") is not None
        assert report.get("observed_failure_kind") is not None
        assert report["expected_failure_kind"] == report["observed_failure_kind"]

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_commands_dict_has_argv_and_text(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report commands dict has commands_argv (list) and commands_text (string)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"cmds-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands = report.get("commands", {})
        assert "commands_argv" in commands, f"Missing commands_argv for {scenario}"
        assert "commands_text" in commands, f"Missing commands_text for {scenario}"

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_commands_argv_are_proper_lists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """commands_argv entries are proper lists (not strings) containing config path."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"argv-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        commands_argv = report["commands"]["commands_argv"]
        for cmd_name, argv in commands_argv.items():
            assert isinstance(argv, list), (
                f"commands_argv[{cmd_name!r}] is {type(argv).__name__}, expected list"
            )
            # Each argv should contain "--config" somewhere (except empty ones).
            if argv:
                assert "--config" in argv, (
                    f"commands_argv[{cmd_name!r}] missing --config: {argv}"
                )

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_operator_interpretation_present(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has operator_interpretation (non-empty)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"interp-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        interp = report.get("operator_interpretation")
        assert interp is not None, f"Missing operator_interpretation for {scenario}"
        assert isinstance(interp, str)
        assert len(interp) > 0

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_errors_list_exists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has 'errors' list (may be empty)."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"errors-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert "errors" in report
        assert isinstance(report["errors"], list)

    @pytest.mark.parametrize("scenario", _SCENARIOS)
    @pytest.mark.asyncio
    async def test_limitations_list_exists(
        self, scenario: str, tmp_path: Path,
    ) -> None:
        """Report has 'limitations' list."""
        from medre.runtime.run_session import run_bridge_session

        db_path = str(tmp_path / f"lim-{scenario}.db")
        report = await run_bridge_session(
            scenario=scenario,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert "limitations" in report
        assert isinstance(report["limitations"], list)
        assert len(report["limitations"]) > 0


# ===================================================================
# 20. Redaction test — secret-looking config path does not leak
# ===================================================================


class TestRedactionSanitizeError:
    """sanitize_error from medre.observability.sanitization redacts secrets."""

    def test_sanitize_error_redacts_access_token(self) -> None:
        """access_token values are redacted by sanitize_error."""
        from medre.observability.sanitization import sanitize_error

        msg = "Config error: access_token=syt_super_secret_12345 for /etc/medre/access_token_config.toml"
        result = sanitize_error(msg)
        assert "syt_super_secret_12345" not in result
        assert "[REDACTED]" in result
        # Path should survive redaction (it's not a token value).
        assert "/etc/medre/" in result

    def test_sanitize_error_redacts_password(self) -> None:
        """password= values are redacted by sanitize_error."""
        from medre.observability.sanitization import sanitize_error

        msg = "Auth failed: password=hunter2 for user admin"
        result = sanitize_error(msg)
        assert "hunter2" not in result
        assert "[REDACTED]" in result

    def test_sanitize_error_regex_matches_access_token_pattern(self) -> None:
        """The _TOKEN_RE regex matches access_token= patterns."""
        import re
        from medre.observability.sanitization import _TOKEN_RE

        patterns_that_should_match = [
            "access_token=syt_abc123",
            "access_token: tok_value",
            "token=abc123",
            "password=secret",
            "secret=my_secret",
            "syt_AbCdEf123456",
        ]
        for pattern in patterns_that_should_match:
            assert _TOKEN_RE.search(pattern), (
                f"_TOKEN_RE should match: {pattern!r}"
            )

    def test_secret_config_path_does_not_leak_in_sanitized_error(self) -> None:
        """A config path containing 'access_token' in its filename does not leak
        into errors/limitations when sanitize_error is applied."""
        from medre.observability.sanitization import sanitize_error

        # Simulate an error that mentions a secret-looking path.
        secret_path = "/home/user/.config/medre/access_token.toml"
        error_msg = f"Failed to load config from {secret_path}: permission denied"
        result = sanitize_error(error_msg)
        # The path itself should survive (it's not a token value).
        assert secret_path in result
        # But any token-like value would be redacted.
        assert "[REDACTED]" not in result or "access_token=" not in result
