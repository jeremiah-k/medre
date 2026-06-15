"""Tests for 'medre config check' routes integration, config sample, and config errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.config._yaml import parse_yaml_config
from medre.config.loader import load_config
from medre.config.sample import generate_sample_config
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.cli import (
    CONFIG_BAD_LIMITS,
    CONFIG_MINIMAL,
    CONFIG_NO_ROUTES,
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
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_WITH_ROUTES)
    return p


@pytest.fixture()
def config_no_routes(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_NO_ROUTES)
    return p


@pytest.fixture()
def config_minimal(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_MINIMAL)
    return p


@pytest.fixture()
def config_bad_limits(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_BAD_LIMITS)
    return p


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

    def test_config_check_route_on_off_markers(self, config_with_routes: Path) -> None:
        """Config check route inventory shows [ON]/[OFF] markers."""
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "[ON]" in output
        assert "[OFF]" in output

    def test_config_check_route_summary_count(self, config_with_routes: Path) -> None:
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
# config sample — includes routes section
# ---------------------------------------------------------------------------


class TestSampleConfig:
    """Tests for 'medre config sample' including routes section."""

    def test_sample_includes_routes_section(self) -> None:
        output = _run_cli("config", "sample")
        assert "routes:" in output
        assert "source_adapters" in output
        assert "dest_adapters" in output
        assert "directionality" in output

    def test_sample_includes_active_bridge_example(self) -> None:
        """Sample includes a clear Matrix -> Meshtastic bridge example."""
        output = _run_cli("config", "sample")
        assert "matrix_radio_bridge" in output
        assert "source_to_dest" in output or "bidirectional" in output

    def test_sample_includes_disabled_route_example(self) -> None:
        """Sample includes a commented-out disabled route example."""
        output = _run_cli("config", "sample")
        assert "enabled: false" in output

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
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") and "=" not in stripped:
                pytest.fail(f"Sample appears to contain YAML-style list: {line!r}")


# ---------------------------------------------------------------------------
# config check — error / nonzero exit tests
# ---------------------------------------------------------------------------


class TestConfigCheckErrors:
    """Tests that 'medre config check' exits nonzero on invalid config."""

    def test_missing_config_file(self, tmp_path: Path) -> None:
        """Missing config file causes nonzero exit with clear error message."""
        with pytest.raises(SystemExit) as exc_info:
            _run_cli("config", "check", "--config", str(tmp_path / "missing.yaml"))
        assert exc_info.value.code != 0

    def test_missing_config_file_clear_message(self, tmp_path: Path) -> None:
        """Error message is human-readable, not a traceback."""
        _, stderr = _run_cli_both(
            "config", "check", "--config", str(tmp_path / "missing.yaml")
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
        output = _run_cli("config", "check", "--config", str(config_with_routes))
        assert "Config valid" in output


# ---------------------------------------------------------------------------
# config sample — fake-buildable round-trip
# ---------------------------------------------------------------------------


class TestSampleConfigFakeBuildable:
    """The generated sample config must load, validate, and build with fake
    adapters.  This proves the ``medre config sample`` first-run path works
    without any optional SDKs installed."""

    @pytest.fixture(autouse=True)
    def _medre_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    def test_sample_parses_as_yaml(self) -> None:
        """generate_sample_config() produces valid YAML."""
        sample = generate_sample_config()
        data = parse_yaml_config(sample)
        assert isinstance(data, dict)
        assert "runtime" in data
        assert "adapters" in data

    def test_sample_loads_via_config_loader(self, tmp_path: Path) -> None:
        """Sample config loads successfully via load_config()."""
        sample = generate_sample_config()
        config_path = tmp_path / "sample.yaml"
        config_path.write_text(sample)
        config, _source, _paths = load_config(str(config_path))
        assert config.runtime.name == "medre"
        assert config.storage.backend == "sqlite"

    def test_sample_all_adapters_are_fake(self, tmp_path: Path) -> None:
        """All adapters in the sample must be fake (no SDKs required)."""
        sample = generate_sample_config()
        config_path = tmp_path / "sample.yaml"
        config_path.write_text(sample)
        config, _, _ = load_config(str(config_path))
        for _transport, _aid, rtc in config.adapters.all_configs():
            assert rtc.adapter_kind == "fake", (
                f"Expected adapter_kind='fake' for {_transport}.{_aid}, "
                f"got {rtc.adapter_kind!r}"
            )

    def test_sample_builds_via_runtime_builder(self, tmp_path: Path) -> None:
        """Sample config builds a runtime with no build failures."""
        sample = generate_sample_config()
        config_path = tmp_path / "sample.yaml"
        config_path.write_text(sample)
        config, _, paths = load_config(str(config_path))
        builder = RuntimeBuilder(config, paths)
        app = builder.build()
        # Matrix (main) + Meshtastic (radio) are enabled; MeshCore and LXMF
        # are disabled.
        assert len(app.adapters) >= 2, (
            f"Expected >= 2 built adapters, got {len(app.adapters)}: "
            f"{list(app.adapters.keys())}"
        )
        assert (
            app.build_failures == []
        ), f"Unexpected build failures: {app.build_failures}"

    def test_sample_routes_parse_correctly(self, tmp_path: Path) -> None:
        """Routes in sample config parse into RouteConfigSet."""
        sample = generate_sample_config()
        config_path = tmp_path / "sample.yaml"
        config_path.write_text(sample)
        config, _, _ = load_config(str(config_path))
        routes = config.routes
        assert len(routes.routes) >= 1
        route_ids = [r.route_id for r in routes.routes]
        assert "matrix_radio_bridge" in route_ids

    def test_sample_config_check_passes(self, tmp_path: Path) -> None:
        """``medre config check`` on the sample config succeeds."""
        sample = generate_sample_config()
        config_path = tmp_path / "sample.yaml"
        config_path.write_text(sample)
        output = _run_cli("config", "check", "--config", str(config_path))
        assert "Config valid" in output

    def test_sample_no_real_credentials(self) -> None:
        """Sample must not contain real or empty credentials that would
        prevent loading."""
        sample = generate_sample_config()
        # access_token should not be empty string (would fail Matrix validation)
        data = parse_yaml_config(sample)
        for _transport, instances in data.get("adapters", {}).items():
            for _name, conf in instances.items():
                if isinstance(conf, dict) and "access_token" in conf:
                    assert (
                        conf["access_token"] != ""
                    ), "Sample has empty access_token — would fail config validation"
