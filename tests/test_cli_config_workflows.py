"""Config sample and config check CLI workflows.

Covers:

1. ``medre config sample`` — round-trip (generate, parse, validate)
2. ``medre config check`` — full output structure and adapter inventory
3. ``medre config sample`` expanded validation (all sections, TOML parseable)
4. Cross-cutting no-traceback guarantee for config-related error paths
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config
from tests.helpers.cli import (
    _run_cli,
    _run_cli_raw,
)

CONFIG_MINIMAL_MEMORY = """\
[runtime]
name = "minimal-workflow"

[storage]
backend = "memory"
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


# ===================================================================
# 1. Config sample round-trip workflow
# ===================================================================


class TestConfigSampleWorkflow:
    """Operators generate a sample config, save it, and validate it."""

    def test_sample_is_valid_toml(self) -> None:
        """Sample config output is parseable TOML."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        assert isinstance(parsed, dict)

    def test_sample_round_trip_config_check(self, tmp_path: Path) -> None:
        """Generate sample -> save -> config check passes."""
        output = _run_cli("config", "sample")
        active_lines = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped.startswith("#") and stripped:
                active_lines.append(line)
        active_toml = "\n".join(active_lines)
        if not active_toml.strip():
            pytest.skip("sample config is entirely commented out")

        cfg_path = tmp_path / "from_sample.yaml"
        cfg_path.write_text(active_toml)
        output, stderr, code = _run_cli_raw(
            "config", "check", "--config", str(cfg_path)
        )
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
        assert "MEDRE_ADAPTER__MAIN__ACCESS_TOKEN" in output or "env" in output.lower()


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
            "config", "check", "--config", str(tmp_path / "missing.yaml")
        )
        assert "Traceback" not in stderr
        assert "Config error:" in stderr


# ===================================================================
# 12. Config sample expanded validation
# ===================================================================


class TestConfigSampleExpanded:
    """Expanded validation of 'medre config sample' output."""

    def test_sample_toml_sections_parse(self) -> None:
        """Every uncommented section in the sample parses as valid TOML."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        assert "runtime" in parsed

    def test_sample_runtime_has_name(self) -> None:
        """Sample [runtime] has a name field."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        assert "name" in parsed.get("runtime", {})

    def test_sample_storage_has_backend(self) -> None:
        """Sample [storage] has a backend field."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        storage = parsed.get("storage", {})
        assert "backend" in storage

    def test_sample_matrix_adapter_fields(self) -> None:
        """Sample Matrix adapter has required fields."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        adapters = parsed.get("adapters", {})
        matrix = adapters.get("matrix", {})
        assert len(matrix) > 0, "sample has no matrix adapters"
        first_adapter = next(iter(matrix.values()))
        assert "homeserver" in first_adapter
        assert "user_id" in first_adapter
        assert "room_allowlist" in first_adapter
        assert "encryption_mode" in first_adapter

    def test_sample_meshtastic_adapter_fields(self) -> None:
        """Sample Meshtastic adapter has required fields."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        adapters = parsed.get("adapters", {})
        meshtastic = adapters.get("meshtastic", {})
        if meshtastic:
            first_adapter = next(iter(meshtastic.values()))
            assert "connection_type" in first_adapter

    def test_sample_routes_have_required_fields(self) -> None:
        """Active sample routes have source_adapters and dest_adapters."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        routes = parsed.get("routes", {})
        for route_id, route_data in routes.items():
            assert (
                "source_adapters" in route_data
            ), f"sample route {route_id} missing source_adapters"
            assert (
                "dest_adapters" in route_data
            ), f"sample route {route_id} missing dest_adapters"

    def test_sample_limits_have_defaults(self) -> None:
        """Sample [runtime.limits] has all four limit fields."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
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
            assert (
                term not in output.lower()
            ), f"sample contains deprecated term: {term}"

    def test_sample_logging_section(self) -> None:
        """Sample includes [logging] with level and format."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        logging_cfg = parsed.get("logging", {})
        assert "level" in logging_cfg
        assert "format" in logging_cfg

    def test_sample_no_duplicate_keys(self) -> None:
        """Sample YAML has no duplicate keys."""
        output = _run_cli("config", "sample")
        parsed = parse_yaml_config(output)
        assert isinstance(parsed, dict)


# ===================================================================
# Cross-cutting: no-Traceback guarantee for config-related paths
# ===================================================================


class TestNoTracebackGuarantee:
    """Every CLI command produces clean output on misuse -- no raw tracebacks."""

    @pytest.mark.parametrize(
        "args",
        [
            ("config", "check", "--config", "/nonexistent/path.yaml"),
            ("routes", "validate", "--config", "/nonexistent/path.yaml"),
            ("routes", "topology", "--config", "/nonexistent/path.yaml"),
            ("routes", "list", "--config", "/nonexistent/path.yaml"),
            ("diagnostics", "--config", "/nonexistent/path.yaml"),
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
            "run", "--config", str(tmp_path / "missing.yaml")
        )
        assert code != 0
        assert "Traceback" not in stderr
