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
    CONFIG_ROUTE_UNKNOWN_ADAPTERS,
    CONFIG_WITH_ROUTES,
    _run_cli,
    _run_cli_both,
    _run_cli_raw,
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

    def test_sample_is_valid_yaml_not_toml(self) -> None:
        """Sample config is valid YAML with no TOML artifacts or secrets."""
        output = generate_sample_config()
        parsed = parse_yaml_config(output)
        assert isinstance(parsed, dict)
        for line in output.splitlines():
            # no TOML table headers
            assert not line.startswith("["), f"TOML table header: {line!r}"
            # no tab indentation
            assert "\t" not in line, f"Tab in sample: {line!r}"
            # no real-looking secrets
            assert "syt_" not in line, f"Secret token in sample: {line!r}"
            assert "BEGIN PRIVATE KEY" not in line, f"Private key in sample: {line!r}"


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


def test_sample_parses_as_yaml() -> None:
    """generate_sample_config() produces valid YAML."""
    sample = generate_sample_config()
    data = parse_yaml_config(sample)
    assert isinstance(data, dict)
    assert "runtime" in data
    assert "adapters" in data


# ---------------------------------------------------------------------------
# config check — strict validation surfaced via the CLI
# ---------------------------------------------------------------------------


class TestConfigCheckStrictValidation:
    """CLI-level coverage for the strict-validation behaviour added by the
    config-schema-authority-hardening.

    Covers:

    * TOML config rejection with the migration message.
    * YAML parse errors reported with ``path:line:column:`` information.
    * Typed validation errors (unknown root key, unknown adapter key)
      reporting the ``section_path`` and the offending key name.
    * Secret values must never appear in CLI error output — only key names.
    """

    def test_check_rejects_toml_with_migration_message(self, tmp_path: Path) -> None:
        """``medre config check --config <file>.toml`` exits nonzero and the
        error includes the dedicated TOML migration pointer."""
        toml_path = tmp_path / "legacy.toml"
        toml_path.write_text("# legacy TOML config\n")
        _stdout, stderr, code = _run_cli_raw(
            "config", "check", "--config", str(toml_path)
        )
        assert code != 0
        assert "TOML config files are no longer supported" in stderr
        assert "Traceback" not in stderr

    def test_check_yaml_parse_error_includes_path_line_column(
        self, tmp_path: Path
    ) -> None:
        """Malformed YAML produces a clear error carrying path:line:column."""
        import re

        cfg = tmp_path / "broken.yaml"
        # An unclosed flow sequence triggers a YAML parse error.
        cfg.write_text("runtime:\n  name: [unclosed sequence\n")
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        # The error references the file by name.
        assert "broken.yaml" in stderr
        # The strict-YAML formatter produces a ``path:line:column:`` prefix.
        assert re.search(
            r":\d+:\d+:", stderr
        ), f"Expected path:line:column pattern in stderr, got: {stderr!r}"

    def test_check_unknown_root_key_reports_root_section_path(
        self, tmp_path: Path
    ) -> None:
        """Unknown root-level key (typo of ``routes``) is rejected via the CLI
        with the offending key name and a root-config error message."""
        cfg = tmp_path / "unknown_root.yaml"
        cfg.write_text(
            "runtime:\n  name: typo-test\n"
            "roues: {}\n"  # typo of "routes"
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        # The offending key name must appear so the operator can fix the typo.
        assert "roues" in stderr
        # The root section path marker is included for attribution.
        assert "root config key" in stderr

    def test_check_unknown_adapter_key_reports_section_path(
        self, tmp_path: Path
    ) -> None:
        """Unknown adapter-level key is rejected via the CLI with the full
        ``adapters.<transport>.<instance>`` section path."""
        cfg = tmp_path / "unknown_adapter.yaml"
        cfg.write_text(
            "runtime:\n  name: adapter-typo\n"
            "storage:\n  backend: memory\n"
            "adapters:\n"
            "  matrix:\n"
            "    main:\n"
            "      enabled: true\n"
            "      adapter_kind: fake\n"
            "      homeserver: https://fake.local\n"
            "      user_id: '@bot:fake.local'\n"
            "      access_token: tok\n"
            "      room_allowlist: ['!room:fake.local']\n"
            "      encryption_mode: plaintext\n"
            "      bogusextra: true\n"
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        # The full adapter section path is included for attribution.
        assert "adapters.matrix.main" in stderr
        # The offending key name must appear.
        assert "bogusextra" in stderr

    def test_check_error_does_not_leak_redaction_probe(self, tmp_path: Path) -> None:
        """Validation errors must reference key NAMES, never secret VALUES.

        Constructs a config that contains a fake access token AND a
        validation error (unknown root key). The CLI error output must
        mention the offending key (``roues``) but must never include the
        token value, even though the adapter section parses successfully
        before the root-key check fires.
        """
        redaction_probe = "redaction_probe_12345"
        cfg = tmp_path / "secret_leak.yaml"
        cfg.write_text(
            "runtime:\n  name: leak-test\n"
            "roues: {}\n"  # unknown root key triggers the validation error
            "adapters:\n"
            "  matrix:\n"
            "    main:\n"
            "      enabled: true\n"
            "      adapter_kind: fake\n"
            "      homeserver: https://fake.local\n"
            "      user_id: '@bot:fake.local'\n"
            f"      access_token: {redaction_probe}\n"
            "      room_allowlist: ['!room:fake.local']\n"
            "      encryption_mode: plaintext\n"
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        # The offending key name is shown so the operator can act on it.
        assert "roues" in stderr
        # The secret value must NOT appear anywhere in the error output.
        assert (
            redaction_probe not in stderr
        ), f"Secret value leaked into CLI error output: {stderr!r}"


# ---------------------------------------------------------------------------
# config check — section-level strict validation surfaced via the CLI
# (unknown transport / malformed instance / unknown retry key)
# ---------------------------------------------------------------------------


class TestConfigCheckSectionStrictValidation:
    """CLI-level coverage for the section-level strict validation added by
    the config-schema-authority-hardening.

    Each test verifies that ``medre config check``:

    * exits nonzero,
    * emits a clean error message (no Python traceback),
    * names the offending key so the operator can act on it.
    """

    def test_check_rejects_unknown_transport_group(self, tmp_path: Path) -> None:
        """``adapters.matrixx`` exits nonzero with a clean message naming
        the typo'd transport and the valid transport list."""
        cfg = tmp_path / "unknown_transport.yaml"
        cfg.write_text(
            "runtime:\n  name: typo\n"
            "adapters:\n"
            "  matrixx:\n"
            "    main:\n"
            "      enabled: true\n"
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        assert "matrixx" in stderr

    def test_check_rejects_malformed_adapter_instance(self, tmp_path: Path) -> None:
        """``adapters.matrix.main: 'bad'`` exits nonzero with a clean
        message; previously it was silently skipped."""
        cfg = tmp_path / "bad_instance.yaml"
        cfg.write_text(
            "runtime:\n" "  name: typo\n" "adapters:\n" "  matrix:\n" "    main: bad\n"
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        assert "adapters.matrix.main" in stderr

    def test_check_rejects_unknown_global_retry_key(self, tmp_path: Path) -> None:
        """``retry: {bogus: 123}`` exits nonzero with a clean message
        naming the unknown key."""
        cfg = tmp_path / "unknown_retry.yaml"
        cfg.write_text(
            "runtime:\n" "  name: typo\n" "retry:\n" "  bogus: 123\n",
        )
        _stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
        assert code != 0
        assert "Traceback" not in stderr
        assert "bogus" in stderr


# ---------------------------------------------------------------------------
# config sample — structured channel_room_map documentation
# ---------------------------------------------------------------------------


class TestSampleConfigStructuredChannelRoomMap:
    """The generated sample config documents the structured
    ``channel_room_map`` entry shape added for per-context origin labels.

    Operators running ``medre config sample`` should see both the
    ``channel_room_map`` field name and the structured-entry field names
    (``source_origin_label`` / ``dest_origin_label``) so they can discover
    the shape without reading the spec.
    """

    def test_sample_mentions_channel_room_map(self) -> None:
        """Sample output mentions ``channel_room_map`` by name."""
        output = _run_cli("config", "sample")
        assert "channel_room_map" in output

    def test_sample_documents_structured_entry_labels(self) -> None:
        """Sample documents per-entry ``source_origin_label`` and
        ``dest_origin_label`` fields used by the structured CRM shape."""
        output = _run_cli("config", "sample")
        assert "source_origin_label" in output
        assert "dest_origin_label" in output


def test_route_unknown_adapter_ref_exits_nonzero(tmp_path: Path) -> None:
    """A route referencing a nonexistent adapter fails config check (F-016).

    Previously such a config passed ``medre config check`` with exit 0
    and only failed at ``medre run`` startup. The pre-flight gate now
    cross-checks route adapter refs against the configured adapter IDs.
    """
    cfg = tmp_path / "bad_route_ref.yaml"
    cfg.write_text(CONFIG_ROUTE_UNKNOWN_ADAPTERS)
    stdout, stderr, code = _run_cli_raw("config", "check", "--config", str(cfg))
    assert code == 2
    assert "Traceback" not in stderr
    combined = stdout + stderr
    assert "nonexistent" in combined
    assert "also_missing" in combined
